"""Sensors for NetOrbit: one per hub or device, plus a summary."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import NetOrbitConfigEntry
from .const import CONF_MODE, CONF_STALE_HOURS, DOMAIN, STATUSES, VERSION
from .coordinator import NetOrbitCoordinator, Node


async def async_setup_entry(
    hass: HomeAssistant,
    entry: NetOrbitConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create entities now and whenever new devices start being tracked."""
    coordinator = entry.runtime_data
    added: dict[str, NetOrbitNodeSensor] = {}

    @callback
    def _sync() -> None:
        nodes = coordinator.data.nodes if coordinator.data else {}
        new = [NetOrbitNodeSensor(coordinator, key) for key in nodes if key not in added]
        for ent in new:
            added[ent.node_key] = ent
        if new:
            async_add_entities(new)
        gone = [key for key in added if key not in nodes]
        if gone:
            reg = er.async_get(hass)
            for key in gone:
                ent = added.pop(key)
                if ent.entity_id and reg.async_get(ent.entity_id):
                    reg.async_remove(ent.entity_id)

    async_add_entities([NetOrbitSummarySensor(coordinator)])
    _sync()
    entry.async_on_unload(coordinator.async_add_listener(_sync))


def _device_info(entry_id: str, title: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name=title,
        manufacturer="NetOrbit",
        model="Network map",
        sw_version=VERSION,
        entry_type=DeviceEntryType.SERVICE,
    )


class NetOrbitNodeSensor(CoordinatorEntity[NetOrbitCoordinator], SensorEntity):
    """Status of one hub or device: online, recent or stale."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUSES
    _attr_translation_key = "node"
    _attr_has_entity_name = True

    def __init__(self, coordinator: NetOrbitCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self.node_key = key
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = _device_info(entry.entry_id, entry.title)
        node = self._node
        self._attr_name = node.name if node else key

    @property
    def _node(self) -> Node | None:
        data = self.coordinator.data
        return data.nodes.get(self.node_key) if data else None

    @property
    def available(self) -> bool:
        return super().available and self._node is not None

    @property
    def native_value(self) -> str | None:
        node = self._node
        return node.status if node and node.status in STATUSES else None

    @property
    def icon(self) -> str | None:
        node = self._node
        return node.icon if node else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        node = self._node
        if node is None:
            return {}
        return {
            "netorbit": True,
            "kind": node.kind,
            "label": node.name,
            "ip": node.ip,
            "mac": node.mac.upper() if node.mac else None,
            "link": node.link if node.link is None else f"hub:{node.link}",
            "node_key": node.key,
            "hub_type": node.hub_type,
            "sleepy": node.sleepy,
            "last_seen": node.last_seen,
            "first_seen": node.first_seen,
            "hostname": node.hostname,
            "source": node.source,
            "vendor": node.vendor,
            "placement": node.placement if node.kind == "device" else None,
            "hub_source": node.hub_source,
            "ip_changes": node.ip_changes,
            "port": node.port,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        node = self._node
        if node and node.name != self._attr_name:
            self._attr_name = node.name
        super()._handle_coordinator_update()


class NetOrbitSummarySensor(CoordinatorEntity[NetOrbitCoordinator], SensorEntity):
    """How many things are online, plus the list of new devices waiting to be added."""

    _attr_has_entity_name = True
    _attr_translation_key = "summary"
    _attr_icon = "mdi:orbit"
    _attr_native_unit_of_measurement = "devices"
    # Scan progress is only interesting while it happens; keep it out of the history database.
    _unrecorded_attributes = frozenset({"scanning", "scan_phase", "scan_progress", "found"})

    def __init__(self, coordinator: NetOrbitCoordinator) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_summary"
        self._attr_device_info = _device_info(entry.entry_id, entry.title)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Scan progress changes a few times per sweep, between map updates.
        self.async_on_remove(self.coordinator.async_add_scan_listener(self.async_write_ha_state))

    @property
    def native_value(self) -> int:
        data = self.coordinator.data
        if not data:
            return 0
        return sum(1 for n in data.nodes.values() if n.kind == "device" and n.status == "online")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        counts = {s: 0 for s in STATUSES}
        if data:
            for node in data.nodes.values():
                if node.kind == "device":
                    counts[node.status] = counts.get(node.status, 0) + 1
        opts = self.coordinator.opts
        scan = self.coordinator.scan
        return {
            "netorbit": True,
            "kind": "summary",
            "mode": opts[CONF_MODE],
            "stale_hours": opts[CONF_STALE_HOURS],
            "counts": counts,
            "new_devices": [
                {**d, "mac": d["mac"].upper() if d.get("mac") else None}
                for d in (data.new_devices if data else [])
            ],
            "suggested_hubs": data.suggested_hubs if data else [],
            "sources": data.sources if data else [],
            "group_by_type": opts.get("group_by_type", True),
            "last_scan": data.last_scan if data else None,
            "scanning": scan["scanning"],
            "scan_phase": scan["phase"],
            "scan_progress": scan["progress"],
            "found": scan["found"],
            "first_scan_done": self.coordinator.first_scan_done,
            "config_entry_id": self.coordinator.config_entry.entry_id,
        }
