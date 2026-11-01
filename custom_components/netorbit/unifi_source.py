"""Read network topology from Home Assistant's own UniFi integration.

The core `unifi` integration already holds an authenticated aiounifi controller
(`config_entry.runtime_data.api`) that the controller's websocket keeps fresh. Reading it
needs no extra credentials and no second login. `runtime_data.api` is not a public API, so
every access here is defensive: if anything is missing or renamed, the result is empty and
the tracker-based placement keeps working.

Only the attributes below are read (aiounifi 96, Home Assistant 2026.10):

* client: ``mac ip hostname name is_wired last_seen essid ap_mac sw_mac sw_port raw``
* device: ``mac ip name model type uplink`` (``uplink`` is a dict with ``uplink_mac`` and
  ``uplink_remote_port``)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from .logic import normalize_mac

_LOGGER = logging.getLogger(__name__)

UNIFI_DOMAIN = "unifi"

# aiounifi device `type` -> the role it plays on the map.
_DEVICE_KINDS = {
    "ugw": "router", "udm": "router", "uxg": "router", "ucg": "router",
    "usw": "switch",
    "uap": "ap",
}


@dataclass
class UnifiClient:
    """One client the controller knows, with where it is plugged in."""

    mac: str
    ip: str | None = None
    hostname: str | None = None
    uplink_mac: str | None = None  # switch MAC when wired, AP MAC when on Wi-Fi
    port: int | None = None  # switch port (wired only)
    wired: bool = False
    network: str | None = None
    vlan: int | None = None
    ssid: str | None = None
    last_seen: int | None = None
    uplink_name: str | None = None  # name of the device it hangs off (`last_uplink_name`); USW Flex needs it
    uplink_port: int | None = None  # `last_uplink_remote_port`, for when `sw_port` is missing


@dataclass
class UnifiDevice:
    """One UniFi network device and the device it hangs off."""

    mac: str
    kind: str  # router | switch | ap
    ip: str | None = None
    name: str | None = None
    uplink_mac: str | None = None
    uplink_port: int | None = None


@dataclass
class UnifiSnapshot:
    """Everything read from the controller in one pass."""

    clients: list[UnifiClient] = field(default_factory=list)
    devices: list[UnifiDevice] = field(default_factory=list)


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _values(handler: Any) -> list[Any]:
    """The items of an aiounifi handler (a mapping keyed by MAC), or [] if it is not one."""
    try:
        return list(handler.values())
    except Exception:  # noqa: BLE001
        return []


def _parse_client(c: Any) -> UnifiClient | None:
    mac = normalize_mac(getattr(c, "mac", None))
    if not mac:
        return None
    wired = bool(getattr(c, "is_wired", False))
    raw = getattr(c, "raw", None)
    raw = raw if isinstance(raw, dict) else {}
    uplink = normalize_mac(getattr(c, "sw_mac", None) if wired else getattr(c, "ap_mac", None))
    port = _int(getattr(c, "sw_port", None)) if wired else None
    if wired and port is None:
        port = _int(raw.get("last_uplink_remote_port"))
    return UnifiClient(
        mac=mac,
        ip=_str(getattr(c, "ip", None)),
        hostname=_str(getattr(c, "hostname", None)) or _str(getattr(c, "name", None)),
        uplink_mac=uplink,
        port=port if wired else None,
        wired=wired,
        network=_str(raw.get("network")),
        vlan=_int(raw.get("vlan")),
        ssid=None if wired else _str(getattr(c, "essid", None)),
        last_seen=_int(getattr(c, "last_seen", None)),
        uplink_name=_str(raw.get("last_uplink_name")),
        uplink_port=_int(raw.get("last_uplink_remote_port")),
    )


def _parse_device(d: Any) -> UnifiDevice | None:
    mac = normalize_mac(getattr(d, "mac", None))
    kind = _DEVICE_KINDS.get(getattr(d, "type", None))
    if not mac or not kind:
        return None
    try:
        up = getattr(d, "uplink", None)  # aiounifi reads raw["uplink"]: KeyError on a gateway
    except Exception:  # noqa: BLE001
        up = None
    up = up if isinstance(up, dict) else {}
    return UnifiDevice(
        mac=mac,
        kind=kind,
        ip=_str(getattr(d, "ip", None)),
        name=_str(getattr(d, "name", None)),
        uplink_mac=normalize_mac(up.get("uplink_mac")),
        uplink_port=_int(up.get("uplink_remote_port")),
    )


def parse_controller(api: Any) -> UnifiSnapshot:
    """Turn an aiounifi controller into plain dataclasses. A broken item is skipped, not fatal."""
    snap = UnifiSnapshot()
    for raw_client in _values(getattr(api, "clients", None)):
        try:
            client = _parse_client(raw_client)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Skipping a UniFi client that could not be read: %s", err)
            continue
        if client:
            snap.clients.append(client)
    for raw_device in _values(getattr(api, "devices", None)):
        try:
            device = _parse_device(raw_device)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Skipping a UniFi device that could not be read: %s", err)
            continue
        if device:
            snap.devices.append(device)
    return snap


def read_unifi(hass: HomeAssistant) -> UnifiSnapshot:
    """Clients and devices from every loaded `unifi` config entry; empty if none is usable."""
    out = UnifiSnapshot()
    try:
        entries = hass.config_entries.async_entries(UNIFI_DOMAIN)
    except Exception:  # noqa: BLE001
        return out
    for entry in entries:
        if getattr(entry, "state", None) is not ConfigEntryState.LOADED:
            continue
        api = getattr(getattr(entry, "runtime_data", None), "api", None)
        if api is None:
            continue
        snap = parse_controller(api)
        out.clients.extend(snap.clients)
        out.devices.extend(snap.devices)
    return out
