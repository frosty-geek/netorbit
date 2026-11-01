"""Discovery, hub detection, layout inference, ping and status tracking for NetOrbit."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
import ipaddress
import logging
import socket
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from . import manage
from .const import (
    CONF_AUTO_HUBS,
    CONF_CHECK_INTERNET,
    CONF_DEVICES,
    CONF_DISCOVERY_INTERVAL,
    CONF_DISMISSED_HUBS,
    CONF_GATEWAY,
    CONF_GROUP_BY_TYPE,
    CONF_HUBS,
    CONF_IGNORE_RANDOM_MAC,
    CONF_IGNORED,
    CONF_INCLUDE_OFF_SUBNET,
    CONF_MODE,
    CONF_PING_INTERVAL,
    CONF_SNMP_COMMUNITY,
    CONF_SNMP_ENABLED,
    CONF_STALE_HOURS,
    CONF_SUBNET,
    DEFAULT_AUTO_HUBS,
    DEFAULT_DISCOVERY_INTERVAL,
    DEFAULT_IGNORE_RANDOM_MAC,
    DEFAULT_INCLUDE_OFF_SUBNET,
    DEFAULT_MODE,
    DEFAULT_PING_INTERVAL,
    DEFAULT_SNMP_ENABLED,
    DEFAULT_STALE_HOURS,
    DOMAIN,
    EVENT_DEVICE_RECOVERED,
    EVENT_DEVICE_STALE,
    EVENT_INTERNET_DOWN,
    EVENT_INTERNET_UP,
    EVENT_NEW_DEVICE,
    HUB_AP,
    HUB_ICONS,
    HUB_MESH,
    HUB_OTHER,
    HUB_ROUTER,
    HUB_SWITCH,
    INTERNET_DOWN_AFTER,
    INTERNET_KEY,
    INTERNET_TARGETS,
    OFF_SUBNET_PRESENT_S,
    MODE_AUTO,
    PLACE_CONTROLLER,
    PLACE_MANUAL,
    PLACE_NONE,
    PLACE_SNMP,
    ROOT_HUB_ID,
    SNMP_DEFAULT_COMMUNITY,
    SNMP_PROBE_INTERVAL_S,
    STATUS_ONLINE,
    STATUS_STALE,
    STORAGE_VERSION,
)
from .detect import NETWORK_VENDORS, Evidence, classify, pretty_vendor
from .logic import (
    compute_status,
    fallback_name,
    guess_icon,
    guess_icon_from,
    is_random_mac,
    normalize_mac,
    parse_proc_arp,
    rollup_status,
    short_hostname,
    subnet_hosts,
)
from .snmp import SnmpClient
from .mdns import mdns_label, mdns_name
from .sources import client_access_points, ping_names_by_ip, registry_by_mac, ssdp_types_by_ip, tracker_names_by_mac
from .topology import Topology, infer, macs_close
from .unifi_source import UnifiSnapshot, read_unifi

_LOGGER = logging.getLogger(__name__)

PING_COUNT = 2
PING_TIMEOUT = 1.5
SWEEP_TIMEOUT = 1.0
DNS_TIMEOUT = 2.0
DNS_REFRESH = timedelta(hours=12)
# Bump when the way names are looked up changes, so existing installs look again right away.
NAME_LOOKUP_VERSION = 2
AUTO_PLACEMENT_TTL = timedelta(days=7)
SNMP_CONCURRENCY = 32

# Sweep phases shown on the summary sensor while a scan runs, with a rough percentage for each.
# "starting" is the moment between setup finishing and the background sweep taking over.
SCAN_PHASES: dict[str, int] = {"starting": 0, "ping": 5, "arp": 40, "names": 50, "snmp": 70, "layout": 90}


@dataclass
class Node:
    """A hub or device shown on the map."""

    key: str
    kind: str  # "hub" or "device"
    name: str
    ip: str | None = None
    mac: str | None = None
    icon: str = "mdi:lan-connect"
    link: str | None = None  # parent hub id
    sleepy: bool = False
    status: str = "recent"
    last_seen: str | None = None
    first_seen: str | None = None
    hostname: str | None = None
    source: str = "discovered"  # discovered | manual | hub
    hub_type: str | None = None
    vendor: str | None = None
    placement: str = PLACE_NONE  # manual | controller | snmp | none
    hub_source: str | None = None  # for hubs: manual | snmp | controller | detected
    ip_changes: int = 0  # how often DHCP has moved it since NetOrbit started tracking
    port: int | None = None  # switch port it is plugged into, when the controller says
    network: str | None = None  # the controller's network (VLAN) name, when it says
    vlan: int | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class NetOrbitData:
    """Everything the entities need after one update."""

    nodes: dict[str, Node] = field(default_factory=dict)
    new_devices: list[dict[str, Any]] = field(default_factory=list)
    suggested_hubs: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    last_scan: str | None = None


def options_with_defaults(entry: ConfigEntry) -> dict[str, Any]:
    """Merge options over defaults so older entries keep working."""
    opts = dict(entry.options)
    opts.setdefault(CONF_MODE, DEFAULT_MODE)
    opts.setdefault(CONF_PING_INTERVAL, DEFAULT_PING_INTERVAL)
    opts.setdefault(CONF_DISCOVERY_INTERVAL, DEFAULT_DISCOVERY_INTERVAL)
    opts.setdefault(CONF_STALE_HOURS, DEFAULT_STALE_HOURS)
    opts.setdefault(CONF_IGNORE_RANDOM_MAC, DEFAULT_IGNORE_RANDOM_MAC)
    opts.setdefault(CONF_AUTO_HUBS, DEFAULT_AUTO_HUBS)
    opts.setdefault(CONF_SNMP_ENABLED, DEFAULT_SNMP_ENABLED)
    opts.setdefault(CONF_SNMP_COMMUNITY, "")
    opts.setdefault(CONF_GROUP_BY_TYPE, True)
    opts.setdefault(CONF_CHECK_INTERNET, True)
    opts.setdefault(CONF_INCLUDE_OFF_SUBNET, DEFAULT_INCLUDE_OFF_SUBNET)
    opts.setdefault(CONF_HUBS, [])
    opts.setdefault(CONF_DEVICES, {})
    opts.setdefault(CONF_IGNORED, [])
    opts.setdefault(CONF_DISMISSED_HUBS, [])
    if not any(h.get("id") == ROOT_HUB_ID for h in opts[CONF_HUBS]):
        opts[CONF_HUBS] = [
            {"id": ROOT_HUB_ID, "name": "Router", "host": entry.data[CONF_GATEWAY], "type": HUB_ROUTER, "parent": None},
            *opts[CONF_HUBS],
        ]
    return opts


class NetOrbitCoordinator(DataUpdateCoordinator[NetOrbitData]):
    """Pings tracked hosts every interval and sweeps the subnet for new ones."""

    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.opts = options_with_defaults(entry)
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=self.opts[CONF_PING_INTERVAL]),
        )
        self.subnet: str = entry.data[CONF_SUBNET]
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self._seen: dict[str, dict[str, Any]] = {}
        self._initialized = False
        self._last_sweep: datetime | None = None
        self._last_snmp_probe: datetime | None = None
        self._prev_status: dict[str, str] = {}
        self._alive_keys: set[str] = set()  # keys that answered the last ping round
        # Internet check: None until the first answer or INTERNET_DOWN_AFTER misses in a row.
        self._internet_up: bool | None = None
        self._internet_misses = 0
        self._internet_changed: str | None = None  # when it last went up or down
        self._force_sweep = False
        self._force_probe = False
        self._privileged: bool | None = None
        self._sweep_lock = asyncio.Lock()
        self._snmp = SnmpClient(hass)
        self._tables: dict[str, dict[str, int]] = {}
        self._topology = Topology()
        self._controller_ap: dict[str, str] = {}
        self._unifi = UnifiSnapshot()
        self._unifi_ports: dict[str, int] = {}  # client or device mac -> port on its uplink switch
        self._unifi_router_macs: set[str] = set()
        self._unifi_nets: dict[str, tuple[str | None, int | None]] = {}  # client mac -> (network, vlan)
        self._suggestions: list[dict[str, Any]] = []
        self._community_asked: set[str] = set()  # IPs already asked with the custom SNMP community
        self._registry: dict[str, Any] = {}
        self._tracker_names: dict[str, str] = {}
        self._ping_names: dict[str, str] = {}
        self.internal_update = False
        # Setup sets this so its first refresh only pings what is already known; the sweep then
        # runs in the background (see async_background_sweep).
        self.defer_sweep = False
        self.scan: dict[str, Any] = {"scanning": False, "phase": None, "progress": 0, "found": 0}
        self._scan_listeners: list[Callable[[], None]] = []

    # ----- lifecycle -------------------------------------------------------

    async def async_load(self) -> None:
        """Load persisted state and work out how we are allowed to ping."""
        data = await self._store.async_load() or {}
        self._seen = data.get("seen", {})
        self._initialized = data.get("initialized", False)
        # The hub tree from the last sweep, so the map looks the same after a restart while the
        # first sweep is still running.
        self._topology = Topology(hub_parent=dict(data.get("hub_parent") or {}))
        self._privileged = await self._detect_privileges()

    async def _detect_privileges(self) -> bool | None:
        from icmplib import SocketPermissionError, async_ping  # noqa: PLC0415

        for privileged in (True, False):
            try:
                await async_ping("127.0.0.1", count=0, timeout=0, privileged=privileged)
            except SocketPermissionError:
                continue
            except Exception:  # noqa: BLE001
                return privileged
            return privileged
        _LOGGER.error("NetOrbit cannot open an ICMP socket; ping based presence is unavailable")
        return None

    @callback
    def apply_options(self, entry: ConfigEntry) -> None:
        """Use new options without reloading the entry."""
        old_comm = self.opts.get(CONF_SNMP_COMMUNITY)
        self.opts = options_with_defaults(entry)
        self.update_interval = timedelta(seconds=self.opts[CONF_PING_INTERVAL])
        if self.opts.get(CONF_SNMP_COMMUNITY) != old_comm:
            self._force_probe = True
            self._force_sweep = True

    def request_sweep(self, probe: bool = False) -> None:
        """Run a discovery sweep (and optionally an SNMP probe) on the next update."""
        self._force_sweep = True
        self._force_probe = self._force_probe or probe

    def _save(self) -> None:
        self._store.async_delay_save(self._stored, 10)

    def _stored(self) -> dict[str, Any]:
        return {"seen": self._seen, "initialized": self._initialized, "hub_parent": self._topology.hub_parent}

    async def async_shutdown(self) -> None:
        """Write what NetOrbit knows right away on unload, so a reload starts from it."""
        await super().async_shutdown()
        await self._store.async_save(self._stored())

    # ----- scan progress ---------------------------------------------------------

    @property
    def first_scan_done(self) -> bool:
        """True once a full sweep has finished on this install. Saved, so it survives restarts."""
        return self._initialized

    @callback
    def async_add_scan_listener(self, update: Callable[[], None]) -> Callable[[], None]:
        """Call update when the scan progress changes. Returns a function that stops it."""
        self._scan_listeners.append(update)

        def remove() -> None:
            if update in self._scan_listeners:
                self._scan_listeners.remove(update)

        return remove

    @callback
    def set_scan(self, notify: bool = True, **changes: Any) -> None:
        """Update the scan progress. Only the summary sensor listens, and only a few times per sweep."""
        if "phase" in changes and changes["phase"] in SCAN_PHASES:
            changes.setdefault("progress", SCAN_PHASES[changes["phase"]])
        self.scan.update(changes)
        if notify:
            for update in list(self._scan_listeners):
                update()

    async def async_background_sweep(self) -> None:
        """The first sweep after setup. It runs once setup has finished, so Home Assistant's
        startup doesn't wait for it, then refreshes the map with what it found."""
        try:
            await self._sweep(dt_util.utcnow())
        except OSError as err:
            _LOGGER.warning("NetOrbit: network scan failed: %s", err)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("NetOrbit: network scan failed")
        # Draw what was found. If the scan failed, don't try again straight away: the regular
        # update retries it.
        self.defer_sweep = True
        try:
            await self.async_refresh()
        finally:
            self.defer_sweep = False

    # ----- update ------------------------------------------------------------

    async def _async_update_data(self) -> NetOrbitData:
        now = dt_util.utcnow()
        due = self._last_sweep is None or now - self._last_sweep >= timedelta(seconds=self.opts[CONF_DISCOVERY_INTERVAL])
        # A sweep someone asked for always runs (after any sweep in progress). A sweep that is
        # merely due waits while setup is running and while another sweep is still going, so the
        # ping round keeps going in the meantime.
        sweep_due = self._force_sweep or (due and not self.defer_sweep and not self._sweep_lock.locked())
        if self._privileged is None:
            raise UpdateFailed("ICMP sockets are not permitted on this system")
        try:
            if sweep_due:
                await self._sweep(now)
            alive = await self._ping_tracked()
        except OSError as err:
            raise UpdateFailed(f"Network error: {err}") from err

        self._note_internet(now, [k for k, _ in alive if k.startswith(f"{INTERNET_KEY}:")])
        alive = [(k, ip) for k, ip in alive if not k.startswith(f"{INTERNET_KEY}:")]
        for key, ip in alive:
            rec = self._seen.setdefault(key, {"first_seen": now.isoformat()})
            rec["last_seen"] = now.isoformat()
            if ip:
                rec["ip"] = ip
        self._refresh_controller()
        controller_online = self._merge_off_subnet(now)
        self._save()
        self._alive_keys = {k for k, _ in alive} | controller_online
        data = self._build(now, self._alive_keys)
        self._fire_transitions(data)
        return data

    @callback
    def async_rebuild(self) -> None:
        """Redraw the map from what NetOrbit already knows, without pinging.

        Used after a settings change (moving a device, adding a hub) so the map updates at once,
        including devices that a new hub's switch port now places under it.
        A full ping round can take a couple of seconds when some devices don't answer.
        """
        if self.data is None:
            return
        now = dt_util.utcnow()
        if self._tables:
            # Hubs may have changed (a device made a hub, a hub removed), so work the layout out
            # again from the switch tables already in memory. No network traffic.
            self._infer_layout(now)
            self._save()
        data = self._build(now, self._alive_keys)
        self.async_set_updated_data(data)

    async def _sweep(self, now: datetime) -> None:
        """Ping every address, read ARP, then work out hubs and layout."""
        from icmplib import async_multiping  # noqa: PLC0415

        async with self._sweep_lock, self._scanning():
            self._force_sweep = False
            hosts = subnet_hosts(self.subnet)
            results = await async_multiping(
                hosts, count=1, timeout=SWEEP_TIMEOUT, concurrent_tasks=128, privileged=self._privileged
            )
            replied = {h.address for h in results if h.is_alive}
            gateway = self.config_entry.data[CONF_GATEWAY]
            self.set_scan(phase="arp", found=len(replied - {gateway}))
            arp = await self.hass.async_add_executor_job(_read_arp)
            # A machine never has an ARP entry for its own address, so add the host Home
            # Assistant runs on from its own network adapters.
            local = await self._local_addresses()
            arp.update(local)
            ignored = set(self.opts[CONF_IGNORED])
            first_run = not self._initialized
            fresh: list[str] = []
            host_set = self._subnet_set(hosts)

            # One IP per MAC. Right after a DHCP change the ARP table can still hold the old
            # address too, so prefer the address that actually answered.
            mac_ip: dict[str, str] = {}
            for ip, mac in sorted(arp.items(), key=lambda kv: _ip_sort(kv[0])):
                if ip not in host_set:
                    continue
                if mac not in mac_ip or (ip in replied and mac_ip[mac] not in replied):
                    mac_ip[mac] = ip

            opts = manage.copy_options(self.opts)
            changed = self._follow_dhcp(opts, mac_ip, arp, now)

            for mac, ip in sorted(mac_ip.items(), key=lambda kv: _ip_sort(kv[1])):
                if self.opts[CONF_IGNORE_RANDOM_MAC] and is_random_mac(mac):
                    continue
                key = mac
                rec = self._seen.get(key)
                if rec is None:
                    rec = self._seen[key] = {"first_seen": now.isoformat(), "discovered": True}
                    if key not in ignored and not self._is_hub_address(mac, ip):
                        fresh.append(key)
                rec["mac"] = mac
                if ip in local:
                    rec["local"] = True
                if rec.get("ip") and rec["ip"] != ip:
                    rec["ip_changes"] = rec.get("ip_changes", 0) + 1
                    rec["ip_changed_at"] = now.isoformat()
                rec["ip"] = ip
                if ip in replied:
                    rec["last_seen"] = now.isoformat()
            if changed:
                self._commit(opts)

            # Everything that answered or that the router has an address for, apart from the router.
            self.set_scan(phase="names", found=len((replied | set(mac_ip.values())) - {gateway}))
            await self._lookup_vendors()
            await self._resolve_names(now)
            self._registry = registry_by_mac(self.hass)
            self._tracker_names = tracker_names_by_mac(self.hass)
            self._ping_names = ping_names_by_ip(self.hass)
            if self.opts[CONF_SNMP_ENABLED]:
                self.set_scan(phase="snmp")
                await self._snmp_probe(now, replied | set(arp))
            await self._detect_hubs()
            if self.opts[CONF_SNMP_ENABLED] and self.opts.get(CONF_SNMP_COMMUNITY):
                # Gear that detection just suggested may only answer to the custom community.
                # Ask it now, then look again, so it doesn't wait for the next sweep.
                before = set(self._community_asked)
                await self._snmp_probe(now, replied | set(arp))
                if self._community_asked != before:
                    await self._detect_hubs()
            # Read tables after detection so switches found in this sweep count straight away.
            self._tables = await self._read_tables() if self.opts[CONF_SNMP_ENABLED] else {}
            self.set_scan(phase="layout")
            self._infer_layout(now)

            self._last_sweep = now
            self._initialized = True
            self._save()

            if not first_run:
                for key in fresh:
                    rec = self._seen[key]
                    self.hass.bus.async_fire(
                        EVENT_NEW_DEVICE,
                        {"mac": rec.get("mac"), "ip": rec.get("ip"), "name": self._discovered_name(key, rec)},
                    )

    async def _local_addresses(self) -> dict[str, str]:
        """{ip: mac} for this machine's own addresses inside the watched subnet.

        Adapters outside the subnet are skipped, so a container on a Docker bridge network
        doesn't add its own bridge address.
        """
        try:
            from homeassistant.components import network  # noqa: PLC0415

            adapters = await network.async_get_adapters(self.hass)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("NetOrbit: can't read network adapters: %s", err)
            return {}
        net = ipaddress.ip_network(self.subnet, strict=False)
        wanted: dict[str, str] = {}
        for adapter in adapters:
            for addr in adapter.get("ipv4") or []:
                try:
                    if ipaddress.ip_address(addr["address"]) in net:
                        wanted[addr["address"]] = adapter["name"]
                except (KeyError, ValueError):
                    continue
        if not wanted:
            return {}

        def read() -> dict[str, str]:
            out = {}
            for ip, name in wanted.items():
                mac = normalize_mac(_iface_mac(name))
                if mac:
                    out[ip] = mac
            return out

        return await self.hass.async_add_executor_job(read)

    @asynccontextmanager
    async def _scanning(self) -> AsyncIterator[None]:
        """Show a sweep as running on the summary sensor while it runs."""
        self.set_scan(scanning=True, phase="ping", found=0)
        try:
            yield
        except BaseException:
            self.set_scan(scanning=False, phase=None)
            raise
        # The update that follows every sweep pushes this together with the new map.
        self.set_scan(notify=False, scanning=False, phase=None, progress=100)

    def _follow_dhcp(self, opts: dict[str, Any], mac_ip: dict[str, str], arp: dict[str, str], now: datetime) -> bool:
        """Keep hubs and hand-added devices pointed at the right address when DHCP moves them.

        * A hub with a known MAC follows that MAC to its new IP.
        * A hub without a known MAC learns it from its IP once.
        * A device added by IP ("ip:x") is re-keyed to its MAC as soon as we learn it, so its
          name, icon and placement survive the next DHCP change.
        """
        changed = False
        for hub in opts[CONF_HUBS]:
            seen_key = f"hub:{hub['id']}"
            rec = self._seen.setdefault(seen_key, {"first_seen": now.isoformat()})
            mac = hub.get("mac") or rec.get("mac")
            if mac:
                rec["mac"] = mac
                new_ip = mac_ip.get(mac)
                if new_ip and hub.get("host") and new_ip != hub["host"]:
                    _LOGGER.info("NetOrbit: %s moved from %s to %s", hub.get("name"), hub["host"], new_ip)
                    hub["host"] = new_ip
                    rec["ip_changes"] = rec.get("ip_changes", 0) + 1
                    rec["ip_changed_at"] = now.isoformat()
                    changed = True
                if not hub.get("mac"):
                    hub["mac"] = mac
                    changed = True
            elif hub.get("host") and arp.get(hub["host"]):
                rec["mac"] = arp[hub["host"]]
                hub["mac"] = rec["mac"]
                changed = True

        for key in [k for k in opts[CONF_DEVICES] if k.startswith("ip:")]:
            ip = key[3:]
            mac = arp.get(ip)
            if not mac or (self.opts[CONF_IGNORE_RANDOM_MAC] and is_random_mac(mac)):
                continue
            cfg = opts[CONF_DEVICES].pop(key)
            cfg.pop("host", None)
            merged = {**cfg, **opts[CONF_DEVICES].get(mac, {})}
            opts[CONF_DEVICES][mac] = merged
            old = self._seen.pop(key, {})
            rec = self._seen.setdefault(mac, {"first_seen": old.get("first_seen") or now.isoformat(), "discovered": True})
            for f in ("last_seen", "first_seen"):
                if old.get(f) and (not rec.get(f) or (f == "last_seen" and old[f] > rec[f])):
                    rec[f] = old[f]
            if key in opts[CONF_IGNORED]:
                opts[CONF_IGNORED].remove(key)
                opts[CONF_IGNORED].append(mac)
            changed = True
        return changed

    def _commit(self, opts: dict[str, Any]) -> None:
        """Save options NetOrbit changed itself, without triggering a second refresh."""
        self.opts = opts
        self.internal_update = True
        self.hass.config_entries.async_update_entry(self.config_entry, options=opts)

    def _subnet_set(self, hosts: list[str]) -> set[str]:
        cached = getattr(self, "_hosts_cache", None)
        if cached is None or cached[0] != self.subnet:
            cached = (self.subnet, set(hosts))
            self._hosts_cache = cached
        return cached[1]

    async def _lookup_vendors(self) -> None:
        todo = [rec for rec in self._seen.values() if rec.get("mac") and "vendor" not in rec]
        if not todo:
            return
        try:
            import aiooui  # noqa: PLC0415

            if not aiooui.is_loaded():
                await aiooui.async_load()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Vendor lookup unavailable: %s", err)
            return
        for rec in todo:
            mac = rec["mac"]
            rec["vendor"] = None if is_random_mac(mac) else pretty_vendor(aiooui.get_vendor(mac[:8].upper()))

    async def _resolve_names(self, now: datetime) -> None:
        """Reverse DNS, then Bonjour, for devices we have no recent hostname for."""
        todo = []
        for key, rec in self._seen.items():
            ip = rec.get("ip")
            checked = rec.get("dns_checked")
            if not ip:
                continue
            fresh = rec.get("dns_version") == NAME_LOOKUP_VERSION
            if fresh and checked and now - datetime.fromisoformat(checked) < DNS_REFRESH:
                continue
            todo.append((key, ip))
        sem = asyncio.Semaphore(16)

        async def one(key: str, ip: str) -> None:
            async with sem:
                try:
                    name = await asyncio.wait_for(
                        self.hass.async_add_executor_job(_reverse_dns, ip), DNS_TIMEOUT
                    )
                except (TimeoutError, asyncio.TimeoutError):
                    name = None
                host = short_hostname(name)
                if not host:
                    # Many routers don't publish local names. Ask the device itself over Bonjour.
                    host = mdns_label(await mdns_name(ip))
                rec = self._seen[key]
                rec["dns_checked"] = now.isoformat()
                rec["dns_version"] = NAME_LOOKUP_VERSION
                if host:
                    rec["hostname"] = host

        await asyncio.gather(*(one(k, ip) for k, ip in todo))

    # ----- SNMP ----------------------------------------------------------------

    def _communities(self) -> list[str]:
        custom = (self.opts.get(CONF_SNMP_COMMUNITY) or "").strip()
        out = [custom] if custom else []
        if SNMP_DEFAULT_COMMUNITY not in out:
            out.append(SNMP_DEFAULT_COMMUNITY)
        return out

    def _community_hosts(self, hub_ips: set[str]) -> set[str]:
        """IPs allowed to receive a custom SNMP community: hubs, suggested hubs, and network-gear makers."""
        out = {ip for ip in hub_ips if ip}
        out |= {s.get("ip") for s in self._suggestions if s.get("ip")}
        for rec in self._seen.values():
            vendor = (rec.get("vendor") or "").lower()
            if rec.get("ip") and any(v in vendor for v in NETWORK_VENDORS):
                out.add(rec["ip"])
        return out

    async def _snmp_probe(self, now: datetime, live_ips: set[str]) -> None:
        """Hourly: ask every live host whether it speaks SNMP and keeps a MAC table."""
        probe_due = (
            self._force_probe
            or self._last_snmp_probe is None
            or now - self._last_snmp_probe >= timedelta(seconds=SNMP_PROBE_INTERVAL_S)
        )
        by_ip = {rec.get("ip"): key for key, rec in self._seen.items() if rec.get("ip")}
        hub_hosts = {h.get("host"): f"hub:{h['id']}" for h in self.opts[CONF_HUBS] if h.get("host")}
        trusted = self._community_hosts(set(hub_hosts))
        # Devices that just became hubs or suggestions are asked with the custom community on the
        # next sweep instead of waiting an hour.
        fresh = (trusted - self._community_asked) & (live_ips | set(hub_hosts)) if self.opts.get(CONF_SNMP_COMMUNITY) else set()
        if probe_due or fresh:
            targets = sorted(live_ips | set(hub_hosts)) if probe_due else sorted(fresh)
            if probe_due:
                self._force_probe = False
                self._last_snmp_probe = now
            sem = asyncio.Semaphore(SNMP_CONCURRENCY)

            async def probe(ip: str) -> None:
                async with sem:
                    # SNMP v2c sends the community in clear text. Your own community only goes to
                    # hubs and likely network gear; everything else is only asked with the factory default.
                    comms = self._communities() if ip in trusted else [SNMP_DEFAULT_COMMUNITY]
                    for i, comm in enumerate(comms):
                        res = await self._snmp.probe(ip, comm)
                        if res:
                            for key in {by_ip.get(ip), hub_hosts.get(ip)} - {None}:
                                rec = self._seen.setdefault(key, {"first_seen": now.isoformat()})
                                rec["snmp"] = {"descr": res.descr, "name": res.name, "bridge": res.bridge, "community": "custom" if (comm != SNMP_DEFAULT_COMMUNITY) else "default"}
                            return
                    for key in {by_ip.get(ip), hub_hosts.get(ip)} - {None}:
                        self._seen.get(key, {}).pop("snmp", None)

            await asyncio.gather(*(probe(ip) for ip in targets))
            self._community_asked |= {ip for ip in targets if ip in trusted}

    async def _read_tables(self) -> dict[str, dict[str, int]]:
        """Read MAC tables from every hub that is a bridge."""
        tables: dict[str, dict[str, int]] = {}
        for hub in self.opts[CONF_HUBS]:
            host = hub.get("host")
            info = self._snmp_info(hub)
            if not host or not info or not info.get("bridge"):
                continue
            comm = self._communities()[0] if info.get("community") == "custom" else SNMP_DEFAULT_COMMUNITY
            table = await self._snmp.mac_table(host, comm)
            if table:
                tables[hub["id"]] = table
        return tables

    def _snmp_info(self, hub: dict[str, Any]) -> dict[str, Any] | None:
        info = (self._seen.get(f"hub:{hub['id']}") or {}).get("snmp")
        if info:
            return info
        mac = hub.get("mac") or (self._seen.get(f"hub:{hub['id']}") or {}).get("mac")
        return (self._seen.get(mac) or {}).get("snmp") if mac else None

    # ----- hub detection ------------------------------------------------------

    def _hub_macs(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for hub in self.opts[CONF_HUBS]:
            mac = hub.get("mac") or (self._seen.get(f"hub:{hub['id']}") or {}).get("mac")
            if mac:
                out[hub["id"]] = mac
        return out

    def _is_hub_address(self, mac: str | None, ip: str | None) -> bool:
        hub_macs = set(self._hub_macs().values())
        hub_ips = {h.get("host") for h in self.opts[CONF_HUBS] if h.get("host")}
        return bool((mac and mac in hub_macs) or (ip and ip in hub_ips))

    def _hub_for_mac(self, mac: str | None) -> str | None:
        """The hub with this MAC. A near match (a radio MAC a few off the LAN MAC) only counts
        when exactly one hub is that close; access points bought together often have
        consecutive MACs and must not be confused."""
        if not mac:
            return None
        hubs = self._hub_macs()
        for hub_id, hmac in hubs.items():
            if hmac == mac:
                return hub_id
        if mac in self._unifi_router_macs:
            return ROOT_HUB_ID  # the gateway's own MAC is often not in ARP (it is not on this VLAN)
        near = [hub_id for hub_id, hmac in hubs.items() if macs_close(hmac, mac)]
        return near[0] if len(near) == 1 else None

    async def _detect_hubs(self) -> None:
        """Add obvious hubs, and collect less certain ones as suggestions."""
        ssdp = await ssdp_types_by_ip(self.hass)
        dismissed = set(self.opts[CONF_DISMISSED_HUBS])
        ignored = set(self.opts[CONF_IGNORED])
        adds: list[dict[str, Any]] = []
        suggestions: list[dict[str, Any]] = []

        candidates: dict[str, dict[str, Any]] = {}
        for key, rec in self._seen.items():
            if key.startswith("hub:") or not rec.get("mac") or not rec.get("discovered") or rec.get("local"):
                continue
            candidates[rec["mac"]] = rec
        # APs that a controller integration reports clients against are hubs by definition.
        self._refresh_controller()
        controller_aps = set(self._controller_ap.values())
        unifi_kinds = {d.mac: d for d in self._unifi.devices if d.kind != "router"}

        # UniFi switches and APs are hubs by definition, even on a VLAN this host can't see.
        for mac, dev in unifi_kinds.items():
            if mac in dismissed or mac in ignored or self._is_hub_address(mac, dev.ip):
                continue
            rec = candidates.get(mac) or {}
            hub_type = HUB_SWITCH if dev.kind == "switch" else HUB_AP
            name = dev.name or self._hub_name(mac, rec, hub_type)
            host = dev.ip or rec.get("ip")
            if self.opts[CONF_AUTO_HUBS]:
                adds.append({"name": name, "type": hub_type, "host": host, "mac": mac, "source": "controller"})
            else:
                suggestions.append({"key": mac, "mac": mac.upper(), "ip": host, "name": name, "type": hub_type,
                                    "reasons": ["the UniFi controller lists it"]})

        for mac, rec in candidates.items():
            if mac in unifi_kinds or mac in dismissed or mac in ignored or self._is_hub_address(mac, rec.get("ip")):
                continue
            reg = self._registry.get(mac)
            snmp = rec.get("snmp") or {}
            ev = Evidence(
                mac=mac, ip=rec.get("ip"), hostname=rec.get("hostname"), vendor=rec.get("vendor"),
                registry_model=reg.model if reg else None, registry_name=reg.name if reg else None,
                registry_domain=reg.domain if reg else None, snmp_bridge=bool(snmp.get("bridge")),
                snmp_descr=snmp.get("descr"), ssdp_types=ssdp.get(rec.get("ip") or "", []),
                label=self._tracker_names.get(mac) or self._ping_names.get(rec.get("ip") or ""),
            )
            verdict = classify(ev)
            if mac in controller_aps:
                verdict.score += 3
                verdict.hub_type = verdict.hub_type or HUB_AP
                verdict.reasons.append("clients connect through it")
            if rec.get("ip") == self.config_entry.data[CONF_GATEWAY]:
                continue
            name = self._hub_name(mac, rec, verdict.hub_type)
            if verdict.auto and self.opts[CONF_AUTO_HUBS]:
                source = "snmp" if ev.snmp_bridge else "controller" if reg and reg.domain else "detected"
                adds.append({"name": name, "type": verdict.hub_type, "host": rec.get("ip"), "mac": mac, "source": source})
            elif verdict.suggest or (verdict.auto and not self.opts[CONF_AUTO_HUBS]):
                suggestions.append({
                    "key": mac, "mac": mac.upper(), "ip": rec.get("ip"), "name": name,
                    "type": verdict.hub_type, "reasons": verdict.reasons,
                })
        self._suggestions = suggestions

        if adds:
            opts = manage.copy_options(self.opts)
            added = []
            for a in adds:
                try:
                    manage.add_hub(opts, a["name"], a["type"], None, a["host"], a["mac"], source=a["source"])
                    added.append(a["name"])
                except manage.ManageError:
                    continue
            if added:
                _LOGGER.info("NetOrbit added hubs: %s", ", ".join(added))
                self._commit(opts)

    def _hub_name(self, mac: str, rec: dict[str, Any], hub_type: str | None) -> str:
        reg = self._registry.get(mac)
        if reg and reg.name:
            return reg.name
        if self._tracker_names.get(mac):
            return self._tracker_names[mac]
        if rec.get("ip") and self._ping_names.get(rec["ip"]):
            return self._ping_names[rec["ip"]]
        snmp_name = (rec.get("snmp") or {}).get("name")
        if snmp_name:
            return snmp_name
        if rec.get("hostname"):
            return rec["hostname"]
        kind = {HUB_AP: "access point", HUB_SWITCH: "switch", HUB_MESH: "mesh node"}.get(hub_type or "", "hub")
        vendor = rec.get("vendor")
        return f"{vendor} {kind} {mac[-5:].upper()}" if vendor else f"{kind.capitalize()} {mac[-5:].upper()}"

    # ----- layout ----------------------------------------------------------------

    def _infer_layout(self, now: datetime) -> None:
        """Turn switch tables into hub parents and device placements, and remember them."""
        hub_macs = self._hub_macs()
        gateway_mac = hub_macs.get(ROOT_HUB_ID)
        device_macs = {k: r["mac"] for k, r in self._seen.items() if r.get("mac") and not k.startswith("hub:")}
        leaf = {h["id"] for h in self.opts[CONF_HUBS] if h.get("type") in (HUB_AP, HUB_MESH, HUB_OTHER) and h["id"] not in self._tables}
        self._topology = infer(self._tables, hub_macs, gateway_mac, device_macs, ROOT_HUB_ID, leaf_hubs=leaf)
        stamp = now.isoformat()
        for key, hub_id in self._topology.device_hub.items():
            self._seen[key]["auto"] = {"hub": hub_id, "source": PLACE_SNMP, "at": stamp}

    def _refresh_controller(self) -> None:
        """Re-read what the controller integrations say about who is plugged in where.

        UniFi (from HA's own `unifi` entry) wins over device trackers: it also knows wired clients.
        """
        self._unifi = read_unifi(self.hass)
        aps = client_access_points(self.hass)
        ports: dict[str, int] = {}
        self._unifi_nets = {c.mac: (c.network, c.vlan) for c in self._unifi.clients if c.network or c.vlan is not None}
        # USW Flex reports a client's `sw_mac` as something that is not the switch's own MAC (and
        # sometimes none), so a MAC that is no hub falls back to the uplink's name.
        by_name = {d.name.casefold(): d.mac for d in self._unifi.devices if d.name}
        for c in self._unifi.clients:
            uplink = c.uplink_mac
            if (not uplink or not self._hub_for_mac(uplink)) and c.uplink_name:
                uplink = by_name.get(c.uplink_name.casefold()) or uplink
            if uplink:
                aps[c.mac] = uplink
                if c.port is not None:
                    ports[c.mac] = c.port
        for d in self._unifi.devices:
            if d.uplink_port is not None:
                ports[d.mac] = d.uplink_port
        self._controller_ap = aps
        self._unifi_ports = ports
        self._unifi_router_macs = {d.mac for d in self._unifi.devices if d.kind == "router"}

    def _merge_off_subnet(self, now: datetime) -> set[str]:
        """Add controller clients that sit outside the swept subnet; return the keys present now.

        They are keyed by MAC like any discovered device but marked `controller_only`: nothing can
        ping them from here, so presence comes from the controller's `last_seen` instead. With the
        option off, anything added earlier is dropped again.
        """
        if not self.opts[CONF_INCLUDE_OFF_SUBNET]:
            for key in [k for k, r in self._seen.items() if r.get("controller_only")]:
                if self._seen[key].pop("controller_only") and self._seen[key].pop("controller_created", False):
                    del self._seen[key]  # NetOrbit only knew it from the controller
            return set()
        try:
            subnet = ipaddress.ip_network(self.subnet, strict=False)
        except ValueError:
            return set()
        hub_macs = set(self._hub_macs().values())
        online: set[str] = set()
        for c in self._unifi.clients:
            if c.mac in hub_macs or not c.ip:
                continue
            try:
                if ipaddress.ip_address(c.ip) in subnet:
                    # The sweep and ping cover it now (it may have come back from another VLAN).
                    for flag in ("controller_only", "controller_created"):
                        (self._seen.get(c.mac) or {}).pop(flag, None)
                    continue
            except ValueError:
                continue
            is_new = c.mac not in self._seen
            rec = self._seen.setdefault(c.mac, {"first_seen": now.isoformat(), "discovered": True})
            if is_new:
                rec["controller_created"] = True
            rec.update({"mac": c.mac, "ip": c.ip, "controller_only": True, "discovered": True})
            if c.hostname:
                rec["hostname"] = c.hostname
            age = now.timestamp() - c.last_seen if c.last_seen else None
            if age is None or age <= OFF_SUBNET_PRESENT_S:
                rec["last_seen"] = now.isoformat()
                online.add(c.mac)
            elif not rec.get("last_seen"):
                rec["last_seen"] = datetime.fromtimestamp(c.last_seen, tz=now.tzinfo).isoformat()
        return online

    def _unifi_hub_parent(self, hub: dict[str, Any], rec: dict[str, Any]) -> str | None:
        """The hub a UniFi device hangs off, from its uplink."""
        mac = hub.get("mac") or rec.get("mac")
        dev = next((d for d in self._unifi.devices if d.mac == mac), None) if mac else None
        parent = self._hub_for_mac(dev.uplink_mac) if dev else None
        return parent if parent != hub["id"] else None

    def _auto_placement(self, key: str, rec: dict[str, Any], now: datetime) -> tuple[str | None, str]:
        mac = rec.get("mac")
        ap = self._controller_ap.get(mac) if mac else None
        hub = self._hub_for_mac(ap)
        if hub:
            rec["auto"] = {"hub": hub, "source": PLACE_CONTROLLER, "at": now.isoformat()}
            return hub, PLACE_CONTROLLER
        auto = rec.get("auto")
        if auto and auto.get("at") and now - datetime.fromisoformat(auto["at"]) < AUTO_PLACEMENT_TTL:
            return auto.get("hub"), auto.get("source", PLACE_SNMP)
        return None, PLACE_NONE

    # ----- ping ------------------------------------------------------------------

    async def _ping_tracked(self) -> list[tuple[str, str | None]]:
        """Ping hubs and tracked devices. Returns [(key, ip)] that answered."""
        from icmplib import async_multiping  # noqa: PLC0415

        targets: dict[str, str] = {}
        for hub in self.opts[CONF_HUBS]:
            if hub.get("host"):
                targets[f"hub:{hub['id']}"] = hub["host"]
        for key, ip in self._tracked_targets().items():
            if ip and not (self._seen.get(key) or {}).get("controller_only"):  # off-subnet: the controller says
                targets[key] = ip
        if self.opts[CONF_CHECK_INTERNET]:
            for ip in INTERNET_TARGETS:
                targets[f"{INTERNET_KEY}:{ip}"] = ip
        if not targets:
            return []
        by_ip: dict[str, list[str]] = {}
        for key, ip in targets.items():
            by_ip.setdefault(ip, []).append(key)
        results = await async_multiping(
            list(by_ip), count=PING_COUNT, interval=0.3, timeout=PING_TIMEOUT,
            concurrent_tasks=64, privileged=self._privileged,
        )
        alive: list[tuple[str, str | None]] = []
        for host in results:
            if host.is_alive:
                for key in by_ip.get(host.address, []):
                    alive.append((key, host.address))
        return alive

    def _note_internet(self, now: datetime, answered: list[str]) -> None:
        """Track whether the internet answers. One missed round is not enough to call it down."""
        if not self.opts[CONF_CHECK_INTERNET]:
            self._internet_up, self._internet_misses = None, 0
            return
        if answered:
            self._internet_misses = 0
            up = True
        else:
            self._internet_misses += 1
            up = False if self._internet_misses >= INTERNET_DOWN_AFTER else self._internet_up
        if up is not None and up != self._internet_up:
            self._internet_changed = now.isoformat()
        self._internet_up = up

    # ----- model ---------------------------------------------------------------

    def _tracked_targets(self) -> dict[str, str | None]:
        """Keys of devices being tracked and the IP to ping for each."""
        out: dict[str, str | None] = {}
        devices = self.opts[CONF_DEVICES]
        ignored = set(self.opts[CONF_IGNORED])
        if self.opts[CONF_MODE] == MODE_AUTO:
            for key, rec in self._seen.items():
                if rec.get("discovered") and key not in ignored:
                    if self.opts[CONF_IGNORE_RANDOM_MAC] and is_random_mac(rec.get("mac")):
                        continue
                    out[key] = rec.get("ip")
        for key, cfg in devices.items():
            if key in ignored or not cfg.get("tracked", True):
                out.pop(key, None)
                continue
            out[key] = (self._seen.get(key) or {}).get("ip") or cfg.get("host")
        # Hubs are drawn as hubs, never as devices too.
        for key in list(out):
            rec = self._seen.get(key) or {}
            if self._is_hub_address(rec.get("mac"), out[key]):
                out.pop(key)
        return out

    def _discovered_name(self, key: str, rec: dict[str, Any]) -> str:
        mac = rec.get("mac")
        if mac:
            reg = self._registry.get(mac)
            if reg and reg.name:
                return reg.name
            if self._tracker_names.get(mac):
                return self._tracker_names[mac]
        if rec.get("ip") and self._ping_names.get(rec["ip"]):
            return self._ping_names[rec["ip"]]
        if rec.get("hostname"):
            return rec["hostname"]
        if rec.get("local"):
            return "Home Assistant"
        vendor = rec.get("vendor")
        if vendor and mac:
            return f"{vendor} {mac[-5:].upper()}"
        return fallback_name(mac, rec.get("ip"))

    def _build(self, now: datetime, alive_keys: set[str]) -> NetOrbitData:
        stale_after = timedelta(hours=self.opts[CONF_STALE_HOURS])
        hub_ids = {h["id"] for h in self.opts[CONF_HUBS]}
        nodes: dict[str, Node] = {}
        sources: set[str] = set()

        for hub in self.opts[CONF_HUBS]:
            key = f"hub:{hub['id']}"
            rec = self._seen.get(key, {})
            unifi_parent = self._unifi_hub_parent(hub, rec)
            if hub["id"] == ROOT_HUB_ID:
                parent = None
            elif hub.get("parent_locked") and hub.get("parent") in hub_ids:
                parent = hub["parent"]
            elif self._topology.hub_parent.get(hub["id"]) in hub_ids:
                parent = self._topology.hub_parent[hub["id"]]
            elif unifi_parent in hub_ids:
                parent = unifi_parent
            elif hub.get("parent") in hub_ids:
                parent = hub["parent"]
            else:
                parent = ROOT_HUB_ID
            if parent == hub["id"]:
                parent = ROOT_HUB_ID
            nodes[key] = Node(
                key=key, kind="hub", name=hub.get("name") or hub["id"], ip=hub.get("host"),
                mac=hub.get("mac") or rec.get("mac"),
                icon=hub.get("icon") or HUB_ICONS.get(hub.get("type", HUB_OTHER), "mdi:lan"),
                link=parent, status=self._status(key in alive_keys, rec, now, stale_after),
                last_seen=rec.get("last_seen"), first_seen=rec.get("first_seen"),
                source="hub", hub_type=hub.get("type", HUB_OTHER), hub_source=hub.get("source", "manual"),
                ip_changes=rec.get("ip_changes", 0),
                vendor=(self._seen.get(hub.get("mac") or "") or {}).get("vendor"),
                port=self._unifi_ports.get(hub.get("mac") or rec.get("mac") or "") if parent == unifi_parent else None,
            )
        if self._tables:
            sources.add(PLACE_SNMP)
        if self._controller_ap:
            sources.add(PLACE_CONTROLLER)

        devices = self.opts[CONF_DEVICES]
        for key, ip in self._tracked_targets().items():
            rec = self._seen.get(key, {})
            cfg = devices.get(key, {})
            name = cfg.get("name") or self._discovered_name(key, rec)
            if cfg.get("hub") in hub_ids:
                hub, placement = cfg["hub"], PLACE_MANUAL
                sources.add(PLACE_MANUAL)
            else:
                hub, placement = self._auto_placement(key, rec, now)
                if hub not in hub_ids:
                    hub, placement = ROOT_HUB_ID, PLACE_NONE
            nodes[key] = Node(
                key=key, kind="device", name=name, ip=ip, mac=rec.get("mac") or cfg.get("mac"),
                icon=cfg.get("icon") or ("mdi:home-assistant" if rec.get("local") else None) or guess_icon_from(f"{name} {rec.get('hostname') or ''}", rec.get("vendor")),
                link=hub, sleepy=bool(cfg.get("sleepy", False)),
                status=self._status(key in alive_keys, rec, now, stale_after),
                last_seen=rec.get("last_seen"), first_seen=rec.get("first_seen"),
                hostname=rec.get("hostname"), source="manual" if key in devices else "discovered",
                vendor=rec.get("vendor"), placement=placement, ip_changes=rec.get("ip_changes", 0),
                port=self._unifi_ports.get(rec.get("mac") or "") if placement == PLACE_CONTROLLER else None,
                network=self._unifi_nets.get(rec.get("mac") or "", (None, None))[0],
                vlan=self._unifi_nets.get(rec.get("mac") or "", (None, None))[1],
            )

        # A hub with no address (an unmanaged switch, an AP in bridge mode) can't be pinged,
        # so it takes its status from what's behind it instead of going stale after the window.
        addressless = {f"hub:{h['id']}": h["id"] for h in self.opts[CONF_HUBS] if not h.get("host")}
        if addressless:
            self._rollup_hubs(nodes, addressless)

        if self.opts[CONF_CHECK_INTERNET]:
            # Above the router. Red (stale) only when the router answers and the internet doesn't;
            # with the router down too, NetOrbit can't tell, so the status is left unknown.
            router_up = f"hub:{ROOT_HUB_ID}" in alive_keys
            if self._internet_up:
                status = STATUS_ONLINE
            elif self._internet_up is False and router_up:
                status = STATUS_STALE
            else:
                status = ""
            nodes[INTERNET_KEY] = Node(
                key=INTERNET_KEY, kind=INTERNET_KEY, name="Internet", icon="mdi:web", link=None,
                status=status, last_seen=self._internet_changed if self._internet_up else None,
                source=INTERNET_KEY,
            )

        new_devices = []
        if self.opts[CONF_MODE] != MODE_AUTO:
            ignored = set(self.opts[CONF_IGNORED])
            for key, rec in self._seen.items():
                if not rec.get("discovered") or key in nodes or key in ignored:
                    continue
                if self.opts[CONF_IGNORE_RANDOM_MAC] and is_random_mac(rec.get("mac")):
                    continue
                if self._is_hub_address(rec.get("mac"), rec.get("ip")):
                    continue
                new_devices.append({
                    "key": key, "mac": rec.get("mac"), "ip": rec.get("ip"),
                    "name": self._discovered_name(key, rec), "first_seen": rec.get("first_seen"),
                    "last_seen": rec.get("last_seen"), "vendor": rec.get("vendor"),
                })
            new_devices.sort(key=lambda d: d.get("first_seen") or "", reverse=True)

        hub_mac_set = set(self._hub_macs().values())
        suggestions = [s for s in self._suggestions if s["key"] not in hub_mac_set and s["key"] not in self.opts[CONF_DISMISSED_HUBS]]

        return NetOrbitData(
            nodes=nodes, new_devices=new_devices, suggested_hubs=suggestions, sources=sorted(sources),
            last_scan=self._last_sweep.isoformat() if self._last_sweep else None,
        )

    @staticmethod
    def _rollup_hubs(nodes: dict[str, Node], addressless: dict[str, str]) -> None:
        """Set each addressless hub's status and last seen from its children, deepest hubs first."""
        children: dict[str, list[Node]] = {}
        for node in nodes.values():
            if node.link:
                children.setdefault(node.link, []).append(node)
        done: set[str] = set()

        def resolve(key: str) -> None:
            if key in done:
                return
            done.add(key)  # before recursing, so a bad parent loop can't recurse forever
            kids = children.get(addressless[key], [])
            for kid in kids:
                if kid.key in addressless:
                    resolve(kid.key)
            node = nodes[key]
            node.status = rollup_status([(k.status, k.sleepy) for k in kids])
            seen = [k.last_seen for k in kids if k.last_seen and (k.status == STATUS_ONLINE or not k.sleepy)]
            node.last_seen = max(seen, key=datetime.fromisoformat) if seen else None

        for key in addressless:
            resolve(key)

    @staticmethod
    def _status(online: bool, rec: dict[str, Any], now: datetime, stale_after: timedelta) -> str:
        last = rec.get("last_seen")
        first = rec.get("first_seen")
        return compute_status(
            online,
            datetime.fromisoformat(last) if last else None,
            datetime.fromisoformat(first) if first else now,
            now,
            stale_after,
        )

    def _fire_transitions(self, data: NetOrbitData) -> None:
        for key, node in data.nodes.items():
            prev = self._prev_status.get(key)
            if key == INTERNET_KEY:
                if prev == STATUS_ONLINE and node.status == STATUS_STALE:
                    self.hass.bus.async_fire(EVENT_INTERNET_DOWN, {"since": self._internet_changed})
                elif prev == STATUS_STALE and node.status == STATUS_ONLINE:
                    self.hass.bus.async_fire(EVENT_INTERNET_UP, {"since": self._internet_changed})
                self._prev_status[key] = node.status
                continue
            if prev is not None and prev != node.status:
                payload = {"name": node.name, "ip": node.ip, "mac": node.mac, "kind": node.kind, "last_seen": node.last_seen}
                if node.status == STATUS_STALE and not node.sleepy:
                    self.hass.bus.async_fire(EVENT_DEVICE_STALE, payload)
                elif prev == STATUS_STALE and node.status == STATUS_ONLINE:
                    self.hass.bus.async_fire(EVENT_DEVICE_RECOVERED, payload)
            self._prev_status[key] = node.status

    # ----- lookups used by services, the options flow and AI tools ------------

    def known_device(self, key: str) -> dict[str, Any] | None:
        return self._seen.get(key)

    def find_key(self, mac: str | None = None, ip: str | None = None) -> str | None:
        """Find a device key by MAC or IP."""
        mac = normalize_mac(mac)
        if mac and mac in self._seen:
            return mac
        if ip:
            for key, rec in self._seen.items():
                if rec.get("ip") == ip and not key.startswith("hub:"):
                    return key
        return mac

    def resolve_device(self, ref: str) -> str | None:
        """Find a device from a MAC, IP, key or name (exact name first, then a unique partial match)."""
        if not ref:
            return None
        ref = str(ref).strip()
        key = self.find_key(mac=ref) if normalize_mac(ref) else None
        if key and key in self._seen:
            return key
        key = self.find_key(ip=ref)
        if key and key in self._seen:
            return key
        if ref in self._seen:
            return ref
        nodes = self.data.nodes if self.data else {}
        low = ref.lower()
        exact = [k for k, n in nodes.items() if n.kind == "device" and n.name.lower() == low]
        if len(exact) == 1:
            return exact[0]
        names = {k: self._discovered_name(k, r).lower() for k, r in self._seen.items() if not k.startswith("hub:")}
        exact = [k for k, n in names.items() if n == low]
        if len(exact) == 1:
            return exact[0]
        partial = [k for k, n in nodes.items() if n.kind == "device" and low in n.name.lower()]
        return partial[0] if len(partial) == 1 else None

    def resolve_hub(self, ref: str) -> str | None:
        """Find a hub from its id, name, IP or MAC."""
        if not ref:
            return None
        ref = str(ref).strip().removeprefix("hub:")
        low = ref.lower()
        mac = normalize_mac(ref)
        # An exact id wins over a name: the card sends ids, and a renamed hub's old id can equal another hub's name.
        for hub in self.opts[CONF_HUBS]:
            if hub["id"] == ref:
                return hub["id"]
        for hub in self.opts[CONF_HUBS]:
            if hub["id"] == ref or (hub.get("name") or "").lower() == low or hub.get("host") == ref:
                return hub["id"]
            if mac and macs_close(hub.get("mac") or (self._seen.get(f"hub:{hub['id']}") or {}).get("mac"), mac):
                return hub["id"]
        partial = [h["id"] for h in self.opts[CONF_HUBS] if low in (h.get("name") or "").lower()]
        return partial[0] if len(partial) == 1 else None

    def match_addresses(self, macs: list[str], ips: list[str]) -> tuple[list[str], list[str]]:
        """Map pasted MACs/IPs to known device keys. Returns (matched keys, unmatched addresses)."""
        matched: list[str] = []
        unmatched: list[str] = []
        for mac in macs:
            key = mac if mac in self._seen else None
            if key is None:
                # A radio MAC in the list might be one byte off the device's real MAC.
                for k, rec in self._seen.items():
                    if not k.startswith("hub:") and macs_close(rec.get("mac"), mac, spread=0):
                        key = k
                        break
            if key and key not in matched and not self._is_hub_address(key, None):
                matched.append(key)
            elif key is None:
                unmatched.append(mac.upper())
        for ip in ips:
            key = self.find_key(ip=ip)
            if key and key in self._seen and key not in matched and not self._is_hub_address(None, ip):
                matched.append(key)
            elif not key:
                unmatched.append(ip)
        return matched, unmatched

    def seed_device(self, mac: str | None, ip: str | None) -> str:
        """Remember a device given by hand (for example from a pasted list) even if no sweep found it yet."""
        key = normalize_mac(mac) or f"ip:{ip}"
        rec = self._seen.setdefault(key, {"first_seen": dt_util.utcnow().isoformat(), "discovered": bool(normalize_mac(mac))})
        if normalize_mac(mac):
            rec["mac"] = normalize_mac(mac)
        if ip:
            rec["ip"] = ip
        return key


def _read_arp() -> dict[str, str]:
    try:
        with open("/proc/net/arp", encoding="ascii") as fh:
            return parse_proc_arp(fh.read())
    except OSError:
        return {}


def _iface_mac(name: str) -> str | None:
    """The MAC of one of this machine's network interfaces, from sysfs."""
    if not name or "/" in name or name.startswith("."):
        return None
    try:
        with open(f"/sys/class/net/{name}/address", encoding="ascii") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _reverse_dns(ip: str) -> str | None:
    try:
        return socket.gethostbyaddr(ip)[0]
    except (OSError, UnicodeError):
        return None


def _ip_sort(ip: str) -> tuple[int, ...]:
    return tuple(int(p) for p in ip.split("."))
