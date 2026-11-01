"""The machine Home Assistant runs on never has an ARP entry for itself (issue #10).

NetOrbit should still put it on the map, using Home Assistant's own network adapters.
All addresses use the documentation ranges.
"""

from __future__ import annotations

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.netorbit.const import DOMAIN

from .conftest import GATEWAY, MAC_B, SUBNET

HOST_IP = "192.0.2.50"
HOST_MAC = "00:00:5e:00:53:50"
DOCKER_IP = "198.51.100.2"  # a bridge network outside the watched subnet
DOCKER_MAC = "00:00:5e:00:53:99"


def _adapter(name: str, ip: str, prefix: int = 24, enabled: bool = True) -> dict:
    return {"name": name, "index": 0, "enabled": enabled, "auto": True, "default": True,
            "ipv4": [{"address": ip, "network_prefix": prefix}], "ipv6": []}


def _entry(**opts) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN, title="NetOrbit", unique_id=SUBNET, data={"subnet": SUBNET, "gateway": GATEWAY},
        options={"mode": "auto", "ping_interval": 60, "discovery_interval": 300, "stale_hours": 24,
                 "ignore_random_mac": True, **opts},
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, adapters: list[dict], macs: dict[str, str]) -> None:
    async def get_adapters(_hass):
        return adapters

    with (
        patch("homeassistant.components.network.async_get_adapters", side_effect=get_adapters),
        patch("custom_components.netorbit.coordinator._iface_mac", side_effect=macs.get, create=True),
    ):
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.state is ConfigEntryState.LOADED


def _devices(hass: HomeAssistant) -> dict[str, dict]:
    return {
        st.attributes["mac"]: dict(st.attributes, state=st.state)
        for st in hass.states.async_all("sensor")
        if st.attributes.get("netorbit") and st.attributes.get("kind") == "device"
    }


async def test_ha_host_is_on_the_map(hass: HomeAssistant, fake_net) -> None:
    # The host answers its own ping, but its own address is never in its ARP table.
    fake_net.alive = {GATEWAY, "192.0.2.11", HOST_IP}
    fake_net.arp = {GATEWAY: "00:00:5e:00:53:01", "192.0.2.11": MAC_B}
    await _setup(hass, _entry(), [_adapter("eth0", HOST_IP)], {"eth0": HOST_MAC})

    devices = _devices(hass)
    assert MAC_B.upper() in devices
    assert HOST_MAC.upper() in devices, "the Home Assistant host is missing from the map"
    host = devices[HOST_MAC.upper()]
    assert host["ip"] == HOST_IP
    assert host["icon"] == "mdi:home-assistant"
    assert host["state"] == "online"
    assert host["link"] == "hub:gateway"


async def test_adapter_outside_the_subnet_is_skipped(hass: HomeAssistant, fake_net) -> None:
    # Docker bridge mode: the container's own address and MAC are on the Docker network, not the LAN.
    fake_net.alive = {GATEWAY, "192.0.2.11"}
    fake_net.arp = {GATEWAY: "00:00:5e:00:53:01", "192.0.2.11": MAC_B}
    await _setup(hass, _entry(), [_adapter("eth0", DOCKER_IP)], {"eth0": DOCKER_MAC})

    devices = _devices(hass)
    assert DOCKER_MAC.upper() not in devices
    assert set(devices) == {MAC_B.upper()}


async def test_unreadable_mac_is_skipped(hass: HomeAssistant, fake_net) -> None:
    fake_net.alive = {GATEWAY, HOST_IP}
    fake_net.arp = {GATEWAY: "00:00:5e:00:53:01"}
    await _setup(hass, _entry(), [_adapter("eth0", HOST_IP)], {})
    assert _devices(hass) == {}


async def test_ha_host_can_be_ignored(hass: HomeAssistant, fake_net) -> None:
    fake_net.alive = {GATEWAY, HOST_IP}
    fake_net.arp = {GATEWAY: "00:00:5e:00:53:01"}
    await _setup(hass, _entry(ignored=[HOST_MAC]), [_adapter("eth0", HOST_IP)], {"eth0": HOST_MAC})
    assert HOST_MAC.upper() not in _devices(hass)


async def test_host_added_by_hand_is_not_doubled(hass: HomeAssistant, fake_net) -> None:
    # The workaround from the issue: added by MAC with a name and icon of its own, or by IP only.
    fake_net.alive = {GATEWAY, HOST_IP}
    fake_net.arp = {GATEWAY: "00:00:5e:00:53:01"}
    devices = {HOST_MAC: {"name": "Server", "icon": "mdi:server", "host": HOST_IP}}
    await _setup(hass, _entry(devices=devices), [_adapter("eth0", HOST_IP)], {"eth0": HOST_MAC})
    shown = _devices(hass)
    assert list(shown) == [HOST_MAC.upper()]
    assert shown[HOST_MAC.upper()]["icon"] == "mdi:server"
    assert shown[HOST_MAC.upper()]["label"] == "Server"


async def test_host_added_by_ip_is_rekeyed(hass: HomeAssistant, fake_net) -> None:
    fake_net.alive = {GATEWAY, HOST_IP}
    fake_net.arp = {GATEWAY: "00:00:5e:00:53:01"}
    devices = {f"ip:{HOST_IP}": {"name": "Server", "host": HOST_IP}}
    await _setup(hass, _entry(devices=devices), [_adapter("eth0", HOST_IP)], {"eth0": HOST_MAC})
    shown = _devices(hass)
    assert list(shown) == [HOST_MAC.upper()]
    assert shown[HOST_MAC.upper()]["label"] == "Server"


async def test_ha_host_with_a_virtual_nic_is_not_taken_for_a_random_mac(hass: HomeAssistant, fake_net) -> None:
    # HAOS in a VM has a locally administered NIC address (52:54:00 on KVM). That is not a phone's
    # privacy MAC: the host must stay on the map even with "ignore random MACs" on.
    vm_mac = "52:54:00:00:53:50"  # example-ok: synthetic test value
    fake_net.alive = {GATEWAY, HOST_IP}
    fake_net.arp = {GATEWAY: "00:00:5e:00:53:01"}
    await _setup(hass, _entry(), [_adapter("eth0", HOST_IP)], {"eth0": vm_mac})
    host = _devices(hass).get(vm_mac.upper())
    assert host is not None, "the Home Assistant host was dropped as a random MAC"
    assert host["ip"] == HOST_IP and host["state"] == "online"
