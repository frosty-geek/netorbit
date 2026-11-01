"""Reading topology from Home Assistant's own UniFi integration (aiounifi-shaped objects)."""

from types import SimpleNamespace

from homeassistant.config_entries import ConfigEntryState

from custom_components.netorbit.unifi_source import (
    UnifiSnapshot,
    parse_controller,
    read_unifi,
)

GW = "00:00:5e:00:53:01"
SW = "00:00:5e:00:53:02"
AP = "00:00:5e:00:53:03"
PC = "00:00:5e:00:53:10"
PHONE = "00:00:5e:00:53:11"
IOT = "00:00:5e:00:53:12"


class _Handler(dict):
    """aiounifi handlers are mappings keyed by MAC; only .values() is used."""


def _client(mac, **kw):
    raw = kw.pop("raw", {})
    base = dict(mac=mac, ip=None, hostname="", name=None, is_wired=False, last_seen=None,
                essid=None, ap_mac=None, sw_mac=None, sw_port=None, raw=raw)
    base.update(kw)
    return SimpleNamespace(**base)


def _device(mac, type_, **kw):
    base = dict(mac=mac, ip="192.0.2.1", name="dev", model="X", type=type_, uplink=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _api(clients=(), devices=()):
    return SimpleNamespace(
        clients=_Handler({c.mac: c for c in clients}),
        devices=_Handler({d.mac: d for d in devices}),
    )


def _hass(api=None, state=ConfigEntryState.LOADED, has_runtime=True):
    entry = SimpleNamespace(state=state)
    if has_runtime:
        entry.runtime_data = SimpleNamespace(api=api)
    return SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda domain: [entry] if domain == "unifi" else []))


# --- clients -----------------------------------------------------------------

def test_wired_client_uses_switch_and_port():
    api = _api(clients=[_client(PC, ip="192.0.2.50", hostname="pc", is_wired=True, sw_mac=SW.upper(), sw_port=7,
                                raw={"network": "Default", "vlan": 0})])
    snap = parse_controller(api)
    (c,) = snap.clients
    assert c.mac == PC
    assert c.uplink_mac == SW  # normalised to lowercase
    assert c.port == 7
    assert c.wired is True
    assert c.hostname == "pc"
    assert c.network == "Default"


def test_wireless_client_uses_ap_and_ssid():
    api = _api(clients=[_client(PHONE, ap_mac=AP, essid="home", last_seen=1700000000)])
    (c,) = parse_controller(api).clients
    assert c.uplink_mac == AP
    assert c.port is None
    assert c.wired is False
    assert c.ssid == "home"
    assert c.last_seen == 1700000000


def test_vlan_and_network_come_from_raw_and_vlan_is_int():
    api = _api(clients=[_client(IOT, ap_mac=AP, raw={"network": "IoT", "vlan": "15"})])
    (c,) = parse_controller(api).clients
    assert (c.network, c.vlan) == ("IoT", 15)


def test_missing_vlan_is_none():
    api = _api(clients=[_client(IOT, ap_mac=AP, raw={})])
    (c,) = parse_controller(api).clients
    assert (c.network, c.vlan) == (None, None)


def test_wired_client_without_switch_has_no_uplink():
    api = _api(clients=[_client(PC, is_wired=True)])
    (c,) = parse_controller(api).clients
    assert c.uplink_mac is None and c.port is None


def test_name_falls_back_to_name_when_no_hostname():
    api = _api(clients=[_client(PC, hostname="", name="Desk PC")])
    (c,) = parse_controller(api).clients
    assert c.hostname == "Desk PC"


def test_invalid_mac_is_skipped():
    api = _api(clients=[_client("not-a-mac"), _client(PC)])
    assert [c.mac for c in parse_controller(api).clients] == [PC]


def test_one_broken_client_does_not_hide_the_rest():
    class Boom:
        mac = PHONE
        raw = {}

        @property
        def is_wired(self):
            raise RuntimeError("aiounifi changed")

    api = _api(clients=[_client(PC, is_wired=True, sw_mac=SW, sw_port=1)])
    api.clients[PHONE] = Boom()
    assert [c.mac for c in parse_controller(api).clients] == [PC]


# --- devices -----------------------------------------------------------------

def test_device_kinds():
    api = _api(devices=[
        _device(GW, "udm", name="router"),
        _device(SW, "usw", name="switch", uplink={"uplink_mac": GW, "uplink_remote_port": 3}),
        _device(AP, "uap", name="ap", uplink={"uplink_mac": SW.upper(), "uplink_remote_port": 5}),
    ])
    by_mac = {d.mac: d for d in parse_controller(api).devices}
    assert by_mac[GW].kind == "router"
    assert by_mac[SW].kind == "switch"
    assert by_mac[AP].kind == "ap"
    assert by_mac[AP].uplink_mac == SW
    assert by_mac[AP].uplink_port == 5
    assert by_mac[SW].uplink_mac == GW


def test_gateway_has_no_uplink():
    api = _api(devices=[_device(GW, "ugw")])
    (d,) = parse_controller(api).devices
    assert d.kind == "router"
    assert d.uplink_mac is None and d.uplink_port is None


def test_unknown_device_type_is_skipped():
    api = _api(devices=[_device(GW, "something-new"), _device(AP, "uap")])
    assert [d.mac for d in parse_controller(api).devices] == [AP]


def test_device_without_uplink_attribute_does_not_crash():
    dev = SimpleNamespace(mac=AP, ip=None, name="ap", model="X", type="uap")
    api = SimpleNamespace(clients=_Handler(), devices=_Handler({AP: dev}))
    (d,) = parse_controller(api).devices
    assert d.uplink_mac is None


# --- the config entry -------------------------------------------------------

def test_read_unifi_returns_loaded_entry_data():
    api = _api(clients=[_client(PC, is_wired=True, sw_mac=SW, sw_port=2)], devices=[_device(SW, "usw")])
    snap = read_unifi(_hass(api))
    assert [c.mac for c in snap.clients] == [PC]
    assert [d.mac for d in snap.devices] == [SW]


def test_read_unifi_no_entries_is_empty():
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_entries=lambda domain: []))
    assert read_unifi(hass) == UnifiSnapshot([], [])


def test_read_unifi_ignores_entry_that_is_not_loaded():
    api = _api(clients=[_client(PC)])
    assert read_unifi(_hass(api, state=ConfigEntryState.SETUP_RETRY)) == UnifiSnapshot([], [])


def test_read_unifi_survives_missing_runtime_data():
    assert read_unifi(_hass(has_runtime=False)) == UnifiSnapshot([], [])


def test_read_unifi_survives_missing_api_handlers():
    hass = _hass(SimpleNamespace())  # api without .clients / .devices
    assert read_unifi(hass) == UnifiSnapshot([], [])


def test_device_whose_uplink_property_raises_keeps_its_place():
    """aiounifi's Device.uplink is `raw["uplink"]`: a gateway has no such key, so it raises KeyError."""
    class Gateway:
        mac = GW
        ip = "192.0.2.1"
        name = "router"
        model = "X"
        type = "udm"
        raw = {}

        @property
        def uplink(self):
            return self.raw["uplink"]

    api = SimpleNamespace(clients=_Handler(), devices=_Handler({GW: Gateway()}))
    (d,) = parse_controller(api).devices
    assert (d.mac, d.kind, d.uplink_mac) == (GW, "router", None)
