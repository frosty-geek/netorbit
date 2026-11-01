"""Run NetOrbit against many generated networks and check it against the answer key.

Hard failures (the test fails):
* any exception or failed update,
* a device shown twice, or shown as both a device and a hub,
* a hub tree with a loop, or links to hubs that do not exist,
* a device placed on the wrong branch of the network (placement must be the true hub,
  an ancestor of it, or an access point sharing the same switch port; see below),
* manual placement not winning,
* real addresses or the SNMP community in diagnostics,
* state lost across a restart.

Measured and reported (pytest -s), not failed: exact placement rate, hubs found, timing.

Known ambiguity: when an access point and wired devices share one port through an
unmanaged switch, the switch table cannot tell them apart. Those devices may be shown
under the access point. That is counted as "shared port", not as wrong.
"""

from __future__ import annotations

import random
import time
from collections import Counter
from datetime import timedelta
from types import SimpleNamespace

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.netorbit.const import DOMAIN
from custom_components.netorbit.diagnostics import async_get_config_entry_diagnostics
from custom_components.netorbit.snmp import ProbeResult

from .sim.netgen import SHAPES, VENDORS, Network, random_network

STATS: Counter = Counter()
TIMES: list[tuple[str, int, float]] = []


def _apply(fake_net, net: Network, rng: random.Random) -> None:
    fake_net.alive = {n.ip for n in net.nodes.values() if n.ip and n.answers_ping}
    fake_net.arp = {n.ip: n.mac for n in net.nodes.values() if n.ip and n.mac and n.ip != "0"}
    fake_net.dns = {n.ip: f"{n.hostname}.lan" for n in net.nodes.values() if n.ip and n.hostname}
    fake_net.vendors = {p.upper(): v for p, v in VENDORS.values() if v}
    fake_net.snmp = {
        n.ip: (n.snmp_community, ProbeResult(descr=f"{n.hostname or 'switch'} managed switch", name=n.hostname, bridge=True))
        for n in net.nodes.values() if n.snmp_community and n.ip
    }
    fake_net.tables = net.mac_tables(rng)


def _unifi_api(net: Network) -> SimpleNamespace:
    """What the HA unifi integration's aiounifi controller exposes for this network."""
    kinds = {"router": "udm", "switch_unmanaged": "usw", "switch_managed": "usw", "ap_unifi": "uap"}
    devices, clients = {}, {}
    for n in net.nodes.values():
        if n.kind in kinds:
            up = net.nodes[n.parent] if n.parent else None
            uplink = {"uplink_mac": up.mac, "uplink_remote_port": n.port_on_parent} if up else None
            devices[n.mac] = SimpleNamespace(mac=n.mac, ip=n.ip, name=n.hostname, model="X", type=kinds[n.kind], uplink=uplink)
        elif n.kind == "device":
            parent = net.nodes[n.parent]
            clients[n.mac] = SimpleNamespace(
                mac=n.mac, ip=n.ip, hostname=n.hostname or "", name=None, is_wired=not n.wireless, last_seen=None,
                essid="home" if n.wireless else None, ap_mac=parent.mac if n.wireless else None,
                sw_mac=None if n.wireless else parent.mac, sw_port=None if n.wireless else n.port_on_parent,
                raw={"network": "Default", "vlan": 0})
    now = int(time.time())
    for r in net.remote:
        parent = net.nodes[r["parent"]]
        clients[r["mac"]] = SimpleNamespace(
            mac=r["mac"], ip=r["ip"], hostname=r["hostname"], name=None, is_wired=not r["wireless"], last_seen=now - r["age_s"],
            essid="iot" if r["wireless"] else None, ap_mac=parent.mac if r["wireless"] else None,
            sw_mac=None if r["wireless"] else parent.mac, sw_port=r["port"],
            raw={"network": r["network"], "vlan": r["vlan"]})
    return SimpleNamespace(clients=clients, devices=devices)


async def _controller(hass: HomeAssistant, net: Network) -> None:
    """A UniFi-like integration: registry entries for APs and trackers reporting each client's AP."""
    if net.controller == "unifi_api":
        ctrl = MockConfigEntry(domain="unifi", title="UniFi")
        ctrl.add_to_hass(hass)
        ctrl.mock_state(hass, ConfigEntryState.LOADED)
        ctrl.runtime_data = SimpleNamespace(api=_unifi_api(net))
        return
    if net.controller != "unifi":
        return
    ctrl = MockConfigEntry(domain="unifi", title="UniFi")
    ctrl.add_to_hass(hass)
    reg = dr.async_get(hass)
    for n in net.nodes.values():
        if n.kind == "ap_unifi" and n.mac:
            reg.async_get_or_create(config_entry_id=ctrl.entry_id, connections={(dr.CONNECTION_NETWORK_MAC, n.mac)},
                                    name=n.hostname, model="U6-Lite", manufacturer="Ubiquiti")
    for i, n in enumerate(net.devices()):
        parent = net.nodes[n.parent]
        if parent.kind == "ap_unifi" and n.wireless:
            # UniFi reports the AP's own MAC. With a single AP, also exercise the radio-MAC fallback.
            only_ap = sum(1 for x in net.nodes.values() if x.kind == "ap_unifi") == 1
            ap_mac = parent.mac[:-2] + f"{(int(parent.mac[-2:], 16) + 3) % 256:02x}" if only_ap else parent.mac
            hass.states.async_set(f"device_tracker.client_{i}", "home", {"mac": n.mac, "ap_mac": ap_mac})


async def _run(hass: HomeAssistant, fake_net, net: Network, *, community_known: bool, seed: int, extra_opts: dict | None = None) -> dict:
    rng = random.Random(seed)
    _apply(fake_net, net, rng)
    await _controller(hass, net)
    opts = {"mode": "auto", "ping_interval": 60, "discovery_interval": 300, "stale_hours": 24, "ignore_random_mac": True, **(extra_opts or {})}
    if community_known and net.community != "public":
        opts["snmp_community"] = net.community
    entry = MockConfigEntry(domain=DOMAIN, title="NetOrbit", unique_id=net.subnet,
                            data={"subnet": net.subnet, "gateway": net.gateway}, options=opts)
    entry.add_to_hass(hass)
    t0 = time.perf_counter()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)  # the first scan runs in the background
    coord = entry.runtime_data
    await coord.async_refresh()
    await hass.async_block_till_done()
    TIMES.append((net.name, len(net.nodes), time.perf_counter() - t0))
    assert coord.last_update_success, coord.last_exception
    return {"entry": entry, "coord": coord}


def _snapshot(hass: HomeAssistant) -> tuple[dict, dict]:
    hubs, devices = {}, {}
    for st in hass.states.async_all("sensor"):
        a = st.attributes
        if not a.get("netorbit") or a.get("kind") in ("summary", "internet"):
            continue
        (hubs if a["kind"] == "hub" else devices)[a["node_key"]] = st
    return hubs, devices


def _true_node(net: Network, mac: str | None, ip: str | None):
    for n in net.nodes.values():
        if (mac and n.mac and n.mac.upper() == mac.upper()) or (ip and n.ip == ip):
            return n
    return None


def _dump(hass: HomeAssistant, label: str) -> None:
    """Save the sensor states so the card can be drawn from them (NETORBIT_DUMP=<dir>)."""
    import json, os  # noqa: E401, PLC0415

    out = os.environ.get("NETORBIT_DUMP")
    if not out:
        return
    states = {
        s.entity_id: {"entity_id": s.entity_id, "state": s.state, "attributes": dict(s.attributes)}
        for s in hass.states.async_all("sensor") if s.attributes.get("netorbit")
    }
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, label.replace(" ", "_") + ".json"), "w", encoding="utf-8") as fh:
        json.dump(states, fh, default=str)


def _check(hass: HomeAssistant, entry, net: Network, *, snmp_ok: bool, label: str) -> None:
    _dump(hass, label)
    hubs, devices = _snapshot(hass)
    coord = entry.runtime_data
    assert coord.last_update_success

    # Hub tree: one root (the router), every parent exists, no loops.
    roots = [k for k, s in hubs.items() if s.attributes["link"] is None]
    assert roots == ["hub:gateway"], f"{label}: roots {roots}"
    assert hubs["hub:gateway"].attributes["ip"] == net.gateway
    for k, s in hubs.items():
        seen, cur = set(), k
        while cur:
            assert cur not in seen, f"{label}: hub loop at {k}"
            seen.add(cur)
            assert cur in hubs, f"{label}: {k} links to missing hub {cur}"
            cur = hubs[cur].attributes["link"]

    # Devices: valid link, no duplicates, never also a hub.
    hub_macs = {s.attributes["mac"] for s in hubs.values() if s.attributes["mac"]}
    hub_ips = {s.attributes["ip"] for s in hubs.values() if s.attributes["ip"]}
    macs = Counter(s.attributes["mac"] for s in devices.values() if s.attributes["mac"])
    assert not [m for m, c in macs.items() if c > 1], f"{label}: duplicate devices"
    for s in devices.values():
        a = s.attributes
        assert a["link"] in hubs, f"{label}: {a['label']} links to {a['link']}"
        assert a["mac"] not in hub_macs and a["ip"] not in hub_ips, f"{label}: {a['label']} is both a device and a hub"
        assert s.state in ("online", "recent", "stale")

    # Coverage: every real device with a stable MAC is on the map, as a device or (misdetected) as a hub.
    shown = {s.attributes["mac"] for s in devices.values()} | hub_macs
    for n in net.devices():
        if n.ip and n.mac and not n.random_mac:
            assert n.mac.upper() in shown, f"{label}: {n.hostname or n.mac} missing from the map"
        if n.random_mac:
            assert n.mac.upper() not in shown, f"{label}: randomized MAC shown"
    for n in net.devices():
        if n.mac and n.mac.upper() in hub_macs:
            STATS["device_misdetected_as_hub"] += 1

    # Hub detection rate (reported).
    for n in net.hubs():
        if n.kind == "router" or not n.ip:
            continue
        STATS["true_hubs"] += 1
        if n.ip in hub_ips:
            STATS["hubs_found"] += 1
        elif any(s.get("ip") == n.ip for s in (coord.data.suggested_hubs if coord.data else [])):
            STATS["hubs_suggested"] += 1
        else:
            why = f"{n.kind}/{n.vendor}/{'host' if n.hostname else 'nohost'}/{'snmp' if n.snmp_community else 'nosnmp'}"
            if n.snmp_community and (snmp_ok or n.snmp_community == "public"):
                why += "/SHOULD_HAVE_FOUND"
            STATS["missed:" + why] += 1

    # Placement soundness.
    net_hub_by_key = {}
    for k, s in hubs.items():
        t = _true_node(net, s.attributes["mac"], s.attributes["ip"])
        if t:
            net_hub_by_key[k] = t
    for s in devices.values():
        a = s.attributes
        t = _true_node(net, a["mac"], a["ip"])
        if t is None:
            continue
        place = a["placement"]
        STATS[f"placement_{place}"] += 1
        if place in ("manual", "none"):
            continue
        guess = net_hub_by_key.get(a["link"])
        assert guess is not None, f"{label}: placed on a hub with no real counterpart"
        path = net.path_to_root(t.key)
        if guess.key == t.parent:
            STATS["placed_exact"] += 1
        elif guess.key in path:
            STATS["placed_ancestor"] += 1
        else:
            # Allowed only when the guess is an AP on the same switch port as the device.
            shared = _same_port(net, guess.key, t.key)
            assert shared, f"{label}: {t.hostname or t.mac} placed under {guess.key}, really under {t.parent}"
            STATS["placed_shared_port"] += 1
    if snmp_ok:
        assert coord.data.sources and "snmp" in coord.data.sources or not _has_tables(net), f"{label}: SNMP not used"

    # Diagnostics never carry real addresses or the community.
    diag = repr(_run_sync_diag(hass, entry))
    for n in list(net.nodes.values())[:50]:
        if n.ip:
            assert n.ip not in diag
        if n.mac:
            assert n.mac.lower() not in diag.lower()
    if net.community != "public":
        assert net.community not in diag


def _has_tables(net: Network) -> bool:
    """True when at least one switch NetOrbit is allowed to ask is there to be read.

    A custom community is only sent to likely network gear (a network maker, or a name that
    gives it away). A switch from a mixed maker with no telling name and a custom community
    is deliberately never asked; the user adds it as a hub by hand.
    """
    from custom_components.netorbit.detect import NETWORK_VENDORS, _kind_from_text

    def askable(n) -> bool:
        if n.snmp_community == "public":
            return True
        vendor = (n.vendor or "").lower()
        return any(v in vendor for v in NETWORK_VENDORS) or bool(n.hostname and _kind_from_text(n.hostname))

    return any(n.snmp_community and askable(n) for n in net.nodes.values())


def _same_port(net: Network, a: str, b: str) -> bool:
    """True when a and b hang off the same port of the nearest managed switch above them."""
    def first_port(key: str):
        path = net.path_to_root(key)
        for i, k in enumerate(path[1:], 1):
            if net.nodes[k].snmp_community:
                return k, net.nodes[path[i - 1]].port_on_parent
        return None
    return first_port(a) is not None and first_port(a) == first_port(b)


_DIAG: dict = {}


def _run_sync_diag(hass, entry):
    return _DIAG.get(entry.entry_id)


async def _diag(hass, entry) -> None:
    _DIAG[entry.entry_id] = await async_get_config_entry_diagnostics(hass, entry)


async def _churn(hass: HomeAssistant, fake_net, freezer, net: Network, rng: random.Random) -> None:
    """DHCP moves some IPs, a few devices leave, a new one arrives; then a sweep runs."""
    devs = [n for n in net.devices() if n.ip]
    used = {n.ip for n in net.nodes.values() if n.ip}
    free = [h for h in _hosts(net.subnet) if h not in used and h != net.gateway]
    rng.shuffle(free)
    for n in rng.sample(devs, k=min(len(devs), max(1, len(devs) // 8))):
        n.ip = free.pop()
    for n in rng.sample(devs, k=min(len(devs), max(1, len(devs) // 10))):
        n.answers_ping = False
    _apply(fake_net, net, rng)
    freezer.tick(timedelta(seconds=301))
    async_fire_time_changed(hass)
    for _ in range(3):
        await hass.async_block_till_done(wait_background_tasks=True)


def _hosts(subnet: str) -> list[str]:
    import ipaddress  # noqa: PLC0415

    return [str(h) for h in ipaddress.ip_network(subnet).hosts()]


# ----- the tests -------------------------------------------------------------------------

@pytest.mark.parametrize("shape", sorted(SHAPES))
async def test_named_shapes(hass: HomeAssistant, fake_net, freezer, shape: str) -> None:
    rng = random.Random(hash(shape) & 0xFFFF)
    net = SHAPES[shape](rng)
    ctx = await _run(hass, fake_net, net, community_known=True, seed=1)
    await _diag(hass, ctx["entry"])
    _check(hass, ctx["entry"], net, snmp_ok=True, label=shape)
    await _churn(hass, fake_net, freezer, net, rng)
    await _diag(hass, ctx["entry"])
    _check(hass, ctx["entry"], net, snmp_ok=True, label=f"{shape} after churn")


@pytest.mark.parametrize("seed", range(150))
async def test_random_networks(hass: HomeAssistant, fake_net, freezer, seed: int) -> None:
    net = random_network(seed)
    rng = random.Random(seed)
    known = seed % 4 != 0  # a quarter of users never enter their SNMP community
    ctx = await _run(hass, fake_net, net, community_known=known, seed=seed)
    await _diag(hass, ctx["entry"])
    _check(hass, ctx["entry"], net, snmp_ok=known or net.community == "public", label=net.name)
    await _churn(hass, fake_net, freezer, net, rng)
    await _diag(hass, ctx["entry"])
    _check(hass, ctx["entry"], net, snmp_ok=known or net.community == "public", label=f"{net.name} after churn")


@pytest.mark.parametrize("seed", range(20))
async def test_manual_always_wins_and_survives_restart(hass: HomeAssistant, fake_net, seed: int) -> None:
    net = random_network(1000 + seed)
    ctx = await _run(hass, fake_net, net, community_known=True, seed=seed)
    entry = ctx["entry"]
    hubs, devices = _snapshot(hass)
    if not devices:
        return
    rng = random.Random(seed)
    targets = rng.sample(sorted(hubs), k=min(3, len(hubs)))
    moved = {}
    for dev_key in rng.sample(sorted(devices), k=min(5, len(devices))):
        hub = rng.choice(targets)
        await hass.services.async_call(DOMAIN, "assign_device", {"device": devices[dev_key].attributes["mac"], "hub": hub.removeprefix("hub:")}, blocking=True)
        moved[devices[dev_key].attributes["mac"]] = hub
    await entry.runtime_data.async_refresh()
    _, devices = _snapshot(hass)
    by_mac = {s.attributes["mac"]: s for s in devices.values()}
    for mac, hub in moved.items():
        assert by_mac[mac].attributes["link"] == hub and by_mac[mac].attributes["placement"] == "manual"
    before = {s.attributes["mac"]: s.attributes["link"] for s in devices.values()}
    hub_names = sorted(s.attributes["label"] for s in _snapshot(hass)[0].values())

    # Restart: unload and set up again from what was saved.
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    await entry.runtime_data.async_refresh()
    hubs2, devices2 = _snapshot(hass)
    assert sorted(s.attributes["label"] for s in hubs2.values()) == hub_names
    after = {s.attributes["mac"]: s.attributes["link"] for s in devices2.values()}
    for mac, hub in moved.items():
        assert after[mac] == hub
    lost = [m for m in before if m not in after]
    assert not lost, f"devices lost on restart: {lost}"


@pytest.fixture(autouse=True)
def fake_unifi_entry(hass: HomeAssistant):
    """The fake `unifi` entry is marked loaded without the integration (and aiounifi) being
    installed, so mark it unloaded again before Home Assistant tries to unload it."""
    yield
    for entry in hass.config_entries.async_entries("unifi"):
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


async def test_unifi_api_builds_hub_tree_and_places_clients(hass: HomeAssistant, fake_net) -> None:
    """Flex switches without SNMP: the controller's uplinks give the hub tree and every placement."""
    net = SHAPES["unifi_flex"](random.Random(3))
    await _run(hass, fake_net, net, community_known=True, seed=3)
    hubs, devices = _snapshot(hass)

    def hub_key(true_key: str) -> str:
        t = net.nodes[true_key]
        (found,) = [k for k, s in hubs.items() if s.attributes["mac"] == t.mac.upper()]
        return found

    assert hubs[hub_key("swA")].attributes["link"] == "hub:gateway"
    assert hubs[hub_key("swB")].attributes["link"] == hub_key("swA")
    assert hubs[hub_key("ap")].attributes["link"] == hub_key("swB")
    by_mac = {s.attributes["mac"]: s.attributes for s in devices.values()}
    for t in net.devices():
        a = by_mac[t.mac.upper()]
        want = "hub:gateway" if t.parent == "router" else hub_key(t.parent)
        assert a["link"] == want, f"{t.hostname or t.mac}: under {a['link']}, really under {want}"
        assert a["placement"] == "controller"
        assert a["port"] == (None if t.wireless else t.port_on_parent)


async def test_unifi_hubs_respect_auto_hubs_off(hass: HomeAssistant, fake_net) -> None:
    """With automatic hubs off, UniFi switches and APs are suggested, not added."""
    net = SHAPES["unifi_flex"](random.Random(3))
    ctx = await _run(hass, fake_net, net, community_known=True, seed=3, extra_opts={"auto_hubs": False})
    hubs, _ = _snapshot(hass)
    assert list(hubs) == ["hub:gateway"]
    assert {sg["mac"] for sg in ctx["coord"].data.suggested_hubs} >= {net.nodes[k].mac.upper() for k in ("swA", "swB", "ap")}


def _remote_by_mac(net: Network, devices: dict) -> dict:
    return {r["mac"]: next((s.attributes for s in devices.values() if s.attributes["mac"] == r["mac"].upper()), None) for r in net.remote}


async def test_off_subnet_clients_are_ignored_by_default(hass: HomeAssistant, fake_net) -> None:
    net = SHAPES["unifi_vlans"](random.Random(3))
    await _run(hass, fake_net, net, community_known=True, seed=3)
    _, devices = _snapshot(hass)
    assert all(a is None for a in _remote_by_mac(net, devices).values())


async def test_off_subnet_clients_from_the_controller(hass: HomeAssistant, fake_net) -> None:
    """Option on: clients on another VLAN appear by MAC, placed by uplink, present by the controller's last_seen."""
    net = SHAPES["unifi_vlans"](random.Random(3))
    pinged: set[str] = set()
    real_multiping = fake_net.multiping

    async def spy(addresses, **kwargs):
        pinged.update(addresses)
        return await real_multiping(addresses, **kwargs)

    fake_net.multiping = spy
    await _run(hass, fake_net, net, community_known=True, seed=3, extra_opts={"include_off_subnet": True})
    hubs, devices = _snapshot(hass)
    remote_ips = {r["ip"] for r in net.remote}
    assert not remote_ips & set(fake_net.alive), "the sim must not make them pingable"
    found = _remote_by_mac(net, devices)
    for r in net.remote:
        a = found[r["mac"]]
        assert a is not None, f"{r['hostname']} missing"
        (want,) = [k for k, s in hubs.items() if s.attributes["mac"] == net.nodes[r["parent"]].mac.upper()]
        assert a["link"] == want and a["placement"] == "controller"
        assert a["port"] == r["port"] if not r["wireless"] else a["port"] is None
        assert (a["network"], a["vlan"]) == ("IoT", 15)
        assert a["ip"] == r["ip"]
    states = {s.attributes["mac"]: s.state for s in devices.values()}
    fresh, old = net.remote[:-2], net.remote[-2:]
    assert all(states[r["mac"].upper()] == "online" for r in fresh)
    assert all(states[r["mac"].upper()] != "online" for r in old)
    # Never pinged: the coordinator must not ask the network about an address it cannot reach.
    assert not remote_ips & pinged


async def test_client_moving_back_into_the_subnet_is_pinged_again(hass: HomeAssistant, fake_net) -> None:
    """A client that comes home from the IoT VLAN must not stay flagged controller-only."""
    net = SHAPES["unifi_vlans"](random.Random(3))
    pinged: set[str] = set()
    real_multiping = fake_net.multiping

    async def spy(addresses, **kwargs):
        pinged.update(addresses)
        return await real_multiping(addresses, **kwargs)

    fake_net.multiping = spy
    ctx = await _run(hass, fake_net, net, community_known=True, seed=3, extra_opts={"include_off_subnet": True})
    coord = ctx["coord"]
    mover = net.remote[0]
    api = hass.config_entries.async_entries("unifi")[0].runtime_data.api
    new_ip = "192.0.2.250"
    api.clients[mover["mac"]].ip = new_ip
    fake_net.alive.add(new_ip)
    fake_net.arp[new_ip] = mover["mac"]
    pinged.clear()
    coord._force_sweep = True
    await coord.async_refresh()
    await hass.async_block_till_done()
    assert new_ip in pinged, "back in the subnet it must be pinged like any other device"
    assert not coord._seen[mover["mac"]].get("controller_only")
    # With the option off it stays: it was a normal device again, not a controller-only record.
    coord.opts["include_off_subnet"] = False
    await coord.async_refresh()
    assert mover["mac"] in coord._seen


async def test_off_subnet_option_off_again_removes_them(hass: HomeAssistant, fake_net) -> None:
    net = SHAPES["unifi_vlans"](random.Random(3))
    ctx = await _run(hass, fake_net, net, community_known=True, seed=3, extra_opts={"include_off_subnet": True})
    coord = ctx["coord"]
    coord.opts["include_off_subnet"] = False
    await coord.async_refresh()
    await hass.async_block_till_done()
    _, devices = _snapshot(hass)
    assert all(a is None for a in _remote_by_mac(net, devices).values())


async def test_manual_placement_beats_unifi(hass: HomeAssistant, fake_net) -> None:
    net = SHAPES["unifi_flex"](random.Random(3))
    ctx = await _run(hass, fake_net, net, community_known=True, seed=3)
    hubs, devices = _snapshot(hass)
    wired = next(t for t in net.devices() if not t.wireless and t.parent == "swB")
    other = next(k for k, s in hubs.items() if s.attributes["mac"] == net.nodes["swA"].mac.upper())
    await hass.services.async_call(DOMAIN, "assign_device", {"device": wired.mac.upper(), "hub": other.removeprefix("hub:")}, blocking=True)
    await ctx["coord"].async_refresh()
    _, devices = _snapshot(hass)
    a = next(s.attributes for s in devices.values() if s.attributes["mac"] == wired.mac.upper())
    assert a["link"] == other and a["placement"] == "manual"


async def test_wrong_community_degrades_gracefully(hass: HomeAssistant, fake_net) -> None:
    net = SHAPES["snmp_chain"](random.Random(7))
    for n in net.nodes.values():
        if n.snmp_community:
            n.snmp_community = "nottheone"
    net.community = "nottheone"
    ctx = await _run(hass, fake_net, net, community_known=False, seed=7)
    await _diag(hass, ctx["entry"])
    _check(hass, ctx["entry"], net, snmp_ok=False, label="wrong community")
    assert "snmp" not in ctx["coord"].data.sources


async def test_report(hass: HomeAssistant) -> None:
    """Print what the runs measured (run last: pytest -s -k report)."""
    if not TIMES:
        return
    placed = STATS["placed_exact"] + STATS["placed_ancestor"] + STATS["placed_shared_port"]
    lines = [
        "",
        "NetOrbit simulation report",
        f"  networks run: {len(TIMES)} (largest {max(n for _, n, _ in TIMES)} nodes)",
        f"  setup+first map: median {sorted(t for *_, t in TIMES)[len(TIMES) // 2] * 1000:.0f} ms, max {max(t for *_, t in TIMES) * 1000:.0f} ms",
        f"  hubs: {STATS['hubs_found']} of {STATS['true_hubs']} found automatically, {STATS['hubs_suggested']} more suggested",
        f"  devices wrongly turned into hubs: {STATS['device_misdetected_as_hub']}",
        f"  automatic placements: {placed} (exact {STATS['placed_exact']}, one level up {STATS['placed_ancestor']}, shared port {STATS['placed_shared_port']})",
        f"  devices left under the router (nothing reported): {STATS['placement_none']}",
    ]
    lines += [f"    missed {k[7:]}: {v}" for k, v in sorted(STATS.items()) if k.startswith("missed:")]
    print("\n".join(lines))


# ----- documentation screenshots ---------------------------------------------------------

DOC_SCENES = ["flat_25", "mesh_22", "unifi_30", "snmp_home", "big_mesh_dhcp", "big_snmp"]


@pytest.mark.parametrize("scene", DOC_SCENES)
async def test_docs_scene(hass: HomeAssistant, fake_net, freezer, scene: str) -> None:
    """Realistic states for README screenshots. Only runs with NETORBIT_DUMP set."""
    import os  # noqa: PLC0415

    from .sim.netgen import flat, mesh_no_data, snmp_chain, unifi  # noqa: PLC0415

    if not os.environ.get("NETORBIT_DUMP"):
        pytest.skip("screenshots only")
    rng = random.Random(scene)
    net = {
        "flat_25": lambda: flat(rng, 25),
        "mesh_22": lambda: mesh_no_data(rng, 3, 22),
        "unifi_30": lambda: unifi(rng, 3, 30),
        "snmp_home": lambda: snmp_chain(rng, 2, 34),
        "big_mesh_dhcp": lambda: mesh_no_data(rng, 4, 360),
        "big_snmp": lambda: snmp_chain(rng, 3, 330),
    }[scene]()
    ctx = await _run(hass, fake_net, net, community_known=True, seed=1)
    devs = [n for n in net.devices() if n.ip and not n.random_mac]
    rng.shuffle(devs)
    # A few devices gone for over a day (red), a few gone for a couple of hours (yellow).
    for n in devs[: max(1, len(devs) // 25)]:
        n.answers_ping = False
    _apply(fake_net, net, rng)
    freezer.tick(timedelta(hours=25))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)
    for n in devs[len(devs) // 25 : len(devs) // 25 + max(1, len(devs) // 15)]:
        n.answers_ping = False
    if "dhcp" in scene:
        for _ in range(3):
            await _churn(hass, fake_net, freezer, net, rng)
    else:
        _apply(fake_net, net, rng)
        freezer.tick(timedelta(seconds=301))
        async_fire_time_changed(hass)
        for _ in range(3):
            await hass.async_block_till_done(wait_background_tasks=True)
    await ctx["coord"].async_refresh()
    _dump(hass, "docs_" + scene)


async def test_flex_switch_clients_are_placed_by_uplink_name(hass: HomeAssistant, fake_net) -> None:
    """USW Flex reports a `sw_mac` that is not the switch's own MAC (or none at all); the name and port still say where."""
    net = SHAPES["unifi_vlans"](random.Random(3))
    ctx = await _run(hass, fake_net, net, community_known=True, seed=3, extra_opts={"include_off_subnet": True})
    coord = ctx["coord"]
    api = hass.config_entries.async_entries("unifi")[0].runtime_data.api
    wired = [r for r in net.remote if not r["wireless"]]
    assert len(wired) >= 2
    for i, r in enumerate(wired):
        c = api.clients[r["mac"]]
        parent = net.nodes[r["parent"]]
        assert parent.hostname, "the sim switch needs a name for this test"
        c.sw_mac = "02:00:00:00:00:99" if i % 2 == 0 else None  # example-ok: synthetic test value
        c.sw_port = r["port"] if i % 2 == 0 else None
        c.raw = {**c.raw, "last_uplink_name": parent.hostname, "last_uplink_remote_port": r["port"]}
    coord._force_sweep = True
    await coord.async_refresh()
    await hass.async_block_till_done()
    hubs, devices = _snapshot(hass)
    found = _remote_by_mac(net, devices)
    for r in wired:
        a = found[r["mac"]]
        (want,) = [k for k, s in hubs.items() if s.attributes["mac"] == net.nodes[r["parent"]].mac.upper()]
        assert a["link"] == want and a["placement"] == "controller", r["hostname"]
        assert a["port"] == r["port"], r["hostname"]
