"""Config and options flows for NetOrbit."""

from __future__ import annotations

import ipaddress
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector as sel

from .const import (
    CONF_AUTO_HUBS,
    CONF_CHECK_INTERNET,
    CONF_DEVICES,
    CONF_DISCOVERY_INTERVAL,
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
    DEFAULT_DISCOVERY_INTERVAL,
    DEFAULT_IGNORE_RANDOM_MAC,
    DEFAULT_MODE,
    DEFAULT_PING_INTERVAL,
    DEFAULT_STALE_HOURS,
    DOMAIN,
    HUB_ROUTER,
    HUB_TYPES,
    MAX_HOSTS,
    MIN_PING_INTERVAL,
    MODES,
    ROOT_HUB_ID,
)
from . import manage
from .coordinator import options_with_defaults
from .logic import extract_addresses, normalize_mac, validate_subnet

# Shown in the "setup finished" dialog (translations can't contain URLs, so it's a placeholder).
GUIDE_URL = "https://github.com/the-scott-davis/netorbit#add-the-map-to-a-dashboard"

NONE_VALUE = "__none__"
AUTO_VALUE = "__auto__"


async def _default_subnet(hass) -> tuple[str, str]:
    """Best guess at the LAN subnet and gateway from Home Assistant's network settings."""
    try:
        from homeassistant.components import network  # noqa: PLC0415

        for adapter in await network.async_get_adapters(hass):
            if not adapter["enabled"]:
                continue
            for ip in adapter["ipv4"]:
                net = ipaddress.ip_network(f"{ip['address']}/{ip['network_prefix']}", strict=False)
                if net.is_private and not net.is_loopback:
                    return str(net), str(next(net.hosts()))
    except Exception:  # noqa: BLE001
        pass
    return "192.168.0.0/24", "192.168.0.1"  # example-ok: fallback when the network can't be detected


def _int_box(minimum: int, maximum: int, unit: str) -> sel.NumberSelector:
    return sel.NumberSelector(
        sel.NumberSelectorConfig(min=minimum, max=maximum, step=1, mode=sel.NumberSelectorMode.BOX, unit_of_measurement=unit)
    )


def _select(options: list[str] | list[dict[str, str]], key: str | None = None, multiple: bool = False) -> sel.SelectSelector:
    config = sel.SelectSelectorConfig(options=options, multiple=multiple, mode=sel.SelectSelectorMode.DROPDOWN)
    if key:
        config["translation_key"] = key
    return sel.SelectSelector(config)


class NetOrbitConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup: which network to watch."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        default_subnet, default_gw = await _default_subnet(self.hass)
        if user_input is not None:
            try:
                subnet = validate_subnet(user_input[CONF_SUBNET], MAX_HOSTS)
            except ValueError as err:
                errors[CONF_SUBNET] = "subnet_too_large" if str(err) == "too_large" else "invalid_subnet"
            else:
                gateway = user_input[CONF_GATEWAY].strip()
                try:
                    if ipaddress.ip_address(gateway) not in ipaddress.ip_network(subnet):
                        errors[CONF_GATEWAY] = "gateway_outside"
                except ValueError:
                    errors[CONF_GATEWAY] = "invalid_ip"
                if not errors:
                    await self.async_set_unique_id(subnet)
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title="NetOrbit",
                        data={CONF_SUBNET: subnet, CONF_GATEWAY: gateway},
                        options={
                            CONF_MODE: user_input[CONF_MODE],
                            CONF_PING_INTERVAL: int(user_input[CONF_PING_INTERVAL]),
                            CONF_DISCOVERY_INTERVAL: DEFAULT_DISCOVERY_INTERVAL,
                            CONF_STALE_HOURS: DEFAULT_STALE_HOURS,
                            CONF_IGNORE_RANDOM_MAC: DEFAULT_IGNORE_RANDOM_MAC,
                            CONF_HUBS: [
                                {"id": ROOT_HUB_ID, "name": "Router", "host": gateway, "type": HUB_ROUTER, "parent": None}
                            ],
                            CONF_DEVICES: {},
                            CONF_IGNORED: [],
                        },
                        description_placeholders={"guide_url": GUIDE_URL},
                    )
        schema = vol.Schema(
            {
                vol.Required(CONF_SUBNET, default=default_subnet): str,
                vol.Required(CONF_GATEWAY, default=default_gw): str,
                vol.Required(CONF_MODE, default=DEFAULT_MODE): _select(MODES, "mode"),
                vol.Required(CONF_PING_INTERVAL, default=DEFAULT_PING_INTERVAL): _int_box(MIN_PING_INTERVAL, 3600, "s"),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> NetOrbitOptionsFlow:
        return NetOrbitOptionsFlow()


class NetOrbitOptionsFlow(OptionsFlow):
    """Settings, hubs and devices."""

    def __init__(self) -> None:
        self._opts: dict[str, Any] | None = None
        self._hub_id: str | None = None
        self._device_key: str | None = None

    @property
    def opts(self) -> dict[str, Any]:
        if self._opts is None:
            self._opts = manage.copy_options(options_with_defaults(self.config_entry))
        return self._opts

    def _coordinator(self):
        return getattr(self.config_entry, "runtime_data", None)

    def _save(self) -> ConfigFlowResult:
        return self.async_create_entry(data=self.opts)

    def _hub_choices(self, exclude: str | None = None) -> list[dict[str, str]]:
        return [
            {"value": h["id"], "label": h.get("name") or h["id"]}
            for h in self.opts[CONF_HUBS]
            if h["id"] != exclude
        ]

    def _tracked_choices(self) -> list[dict[str, str]]:
        coord = self._coordinator()
        if not coord or not coord.data:
            return []
        out = []
        for key, node in sorted(coord.data.nodes.items(), key=lambda kv: kv[1].name.lower()):
            if node.kind == "device":
                out.append({"value": key, "label": f"{node.name} ({node.ip or 'no IP'})"})
        return out

    def _untracked_choices(self) -> list[dict[str, str]]:
        coord = self._coordinator()
        if not coord or not coord.data:
            return []
        tracked = set(coord.data.nodes)
        out = []
        for key, rec in sorted(coord._seen.items(), key=lambda kv: kv[1].get("ip") or ""):  # noqa: SLF001
            if key.startswith("hub:") or key in tracked or not rec.get("discovered"):
                continue
            label = coord._discovered_name(key, rec)  # noqa: SLF001
            out.append({"value": key, "label": f"{label} ({rec.get('ip') or 'no IP'})"})
        return out

    # ----- menu ------------------------------------------------------------------

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        menu = []
        coord = self._coordinator()
        if coord and coord.data and coord.data.suggested_hubs:
            menu.append("review_hubs")
        menu += ["paste_list", "add_hub", "edit_hub", "add_device", "edit_device"]
        if self._untracked_choices():
            menu.append("review_new")
        menu.append("settings")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self.opts.update(
                {
                    CONF_MODE: user_input[CONF_MODE],
                    CONF_PING_INTERVAL: int(user_input[CONF_PING_INTERVAL]),
                    CONF_DISCOVERY_INTERVAL: int(user_input[CONF_DISCOVERY_INTERVAL]),
                    CONF_STALE_HOURS: int(user_input[CONF_STALE_HOURS]),
                    CONF_IGNORE_RANDOM_MAC: user_input[CONF_IGNORE_RANDOM_MAC],
                    CONF_AUTO_HUBS: user_input[CONF_AUTO_HUBS],
                    CONF_GROUP_BY_TYPE: user_input[CONF_GROUP_BY_TYPE],
                    CONF_CHECK_INTERNET: user_input[CONF_CHECK_INTERNET],
                    CONF_INCLUDE_OFF_SUBNET: user_input[CONF_INCLUDE_OFF_SUBNET],
                    CONF_SNMP_ENABLED: user_input[CONF_SNMP_ENABLED],
                    CONF_SNMP_COMMUNITY: (user_input.get(CONF_SNMP_COMMUNITY) or "").strip(),
                }
            )
            return self._save()
        o = self.opts
        schema = vol.Schema(
            {
                vol.Required(CONF_MODE, default=o[CONF_MODE]): _select(MODES, "mode"),
                vol.Required(CONF_PING_INTERVAL, default=o[CONF_PING_INTERVAL]): _int_box(MIN_PING_INTERVAL, 3600, "s"),
                vol.Required(CONF_DISCOVERY_INTERVAL, default=o[CONF_DISCOVERY_INTERVAL]): _int_box(60, 86400, "s"),
                vol.Required(CONF_STALE_HOURS, default=o[CONF_STALE_HOURS]): _int_box(1, 720, "h"),
                vol.Required(CONF_IGNORE_RANDOM_MAC, default=o[CONF_IGNORE_RANDOM_MAC]): bool,
                vol.Required(CONF_AUTO_HUBS, default=o[CONF_AUTO_HUBS]): bool,
                vol.Required(CONF_GROUP_BY_TYPE, default=o[CONF_GROUP_BY_TYPE]): bool,
                vol.Required(CONF_CHECK_INTERNET, default=o[CONF_CHECK_INTERNET]): bool,
                vol.Required(CONF_INCLUDE_OFF_SUBNET, default=o[CONF_INCLUDE_OFF_SUBNET]): bool,
                vol.Required(CONF_SNMP_ENABLED, default=o[CONF_SNMP_ENABLED]): bool,
                vol.Optional(CONF_SNMP_COMMUNITY, description={"suggested_value": o.get(CONF_SNMP_COMMUNITY) or None}): sel.TextSelector(
                    sel.TextSelectorConfig(type=sel.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)

    # ----- hubs ------------------------------------------------------------------

    def _hub_schema(self, hub: dict[str, Any], exclude: str | None, root: bool) -> vol.Schema:
        fields: dict[Any, Any] = {
            vol.Required("name", default=hub.get("name", "")): str,
            vol.Optional("host", description={"suggested_value": hub.get("host")}): str,
            vol.Required("type", default=hub.get("type", "access_point")): _select(HUB_TYPES, "hub_type"),
        }
        if not root:
            fields[vol.Required("parent", default=hub.get("parent") or ROOT_HUB_ID)] = _select(self._hub_choices(exclude))
        return vol.Schema(fields)

    async def async_step_add_hub(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = (user_input.get("host") or "").strip() or None
            if host and not _valid_ip(host):
                errors["host"] = "invalid_ip"
            else:
                try:
                    manage.add_hub(self.opts, user_input["name"], user_input["type"], user_input["parent"], host, None, source="manual")
                except manage.ManageError:
                    errors["host"] = "hub_exists"
                else:
                    return self._save()
        return self.async_show_form(step_id="add_hub", data_schema=self._hub_schema({}, None, False), errors=errors)

    async def async_step_edit_hub(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._hub_id = user_input["hub"]
            return await self.async_step_edit_hub_details()
        return self.async_show_form(
            step_id="edit_hub", data_schema=vol.Schema({vol.Required("hub"): _select(self._hub_choices())})
        )

    async def async_step_edit_hub_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        hub = next(h for h in self.opts[CONF_HUBS] if h["id"] == self._hub_id)
        root = hub["id"] == ROOT_HUB_ID
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("remove") and not root:
                manage.remove_hub(self.opts, hub["id"])
                return self._save()
            host = (user_input.get("host") or "").strip() or None
            if host and not _valid_ip(host):
                errors["host"] = "invalid_ip"
            elif not root and manage.creates_cycle(self.opts[CONF_HUBS], hub["id"], user_input["parent"]):
                errors["parent"] = "hub_cycle"
            else:
                fields = {"name": user_input["name"], "host": host, "type": user_input["type"]}
                if not root and user_input["parent"] != hub.get("parent"):
                    fields["parent"] = user_input["parent"]
                manage.update_hub(self.opts, hub["id"], **fields)
                return self._save()
        schema = self._hub_schema(hub, hub["id"], root)
        if not root:
            schema = schema.extend({vol.Optional("remove", default=False): bool})
        return self.async_show_form(
            step_id="edit_hub_details", data_schema=schema, errors=errors,
            description_placeholders={"hub": hub.get("name") or hub["id"]},
        )

    # ----- devices ---------------------------------------------------------------

    def _device_fields(self, cfg: dict[str, Any]) -> dict[Any, Any]:
        return {
            vol.Optional("name", description={"suggested_value": cfg.get("name")}): str,
            vol.Optional("icon", description={"suggested_value": cfg.get("icon")}): sel.IconSelector(),
            vol.Required("hub", default=cfg.get("hub") or AUTO_VALUE): _select(
                [{"value": AUTO_VALUE, "label": "Automatic"}, *self._hub_choices()]
            ),
            vol.Required("sleepy", default=bool(cfg.get("sleepy", False))): bool,
        }

    async def async_step_add_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            picked = user_input.get("device")
            host = (user_input.get("host") or "").strip() or None
            if picked and picked != NONE_VALUE:
                key = picked
            elif host and _valid_ip(host):
                coord = self._coordinator()
                key = (coord.find_key(ip=host) if coord else None) or f"ip:{host}"
            else:
                errors["base"] = "pick_or_ip"
                key = None
            if key:
                cfg = self.opts[CONF_DEVICES].setdefault(key, {})
                cfg.update(_clean_device(user_input))
                cfg["tracked"] = True
                if host:
                    cfg["host"] = host
                if key in self.opts[CONF_IGNORED]:
                    self.opts[CONF_IGNORED].remove(key)
                return self._save()
        choices = [{"value": NONE_VALUE, "label": "Enter an IP address instead"}, *self._untracked_choices()]
        schema = vol.Schema(
            {
                vol.Optional("device", default=NONE_VALUE): _select(choices),
                vol.Optional("host"): str,
                **self._device_fields({}),
            }
        )
        return self.async_show_form(step_id="add_device", data_schema=schema, errors=errors)

    async def async_step_edit_device(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        choices = self._tracked_choices()
        if not choices:
            return self.async_abort(reason="no_devices")
        if user_input is not None:
            self._device_key = user_input["device"]
            return await self.async_step_edit_device_details()
        return self.async_show_form(
            step_id="edit_device", data_schema=vol.Schema({vol.Required("device"): _select(choices)})
        )

    async def async_step_edit_device_details(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        key = self._device_key
        cfg = dict(self.opts[CONF_DEVICES].get(key, {}))
        coord = self._coordinator()
        node = coord.data.nodes.get(key) if coord and coord.data else None
        if user_input is not None:
            if user_input.get("stop_tracking"):
                self.opts[CONF_DEVICES].pop(key, None)
                if key not in self.opts[CONF_IGNORED]:
                    self.opts[CONF_IGNORED].append(key)
                return self._save()
            new_cfg = self.opts[CONF_DEVICES].setdefault(key, {})
            new_cfg.update(_clean_device(user_input))
            for field in ("name", "icon"):
                if not user_input.get(field):
                    new_cfg.pop(field, None)
            if user_input.get("hub") in (None, AUTO_VALUE):
                manage.reset_placement(self.opts, [key])
            new_cfg["tracked"] = True
            return self._save()
        schema = vol.Schema({**self._device_fields(cfg), vol.Optional("stop_tracking", default=False): bool})
        return self.async_show_form(
            step_id="edit_device_details", data_schema=schema,
            description_placeholders={"device": node.name if node else key, "ip": (node.ip if node else None) or "unknown"},
        )

    async def async_step_review_new(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        choices = self._untracked_choices()
        if not choices:
            return self.async_abort(reason="no_new_devices")
        if user_input is not None:
            for key in user_input.get("track", []):
                self.opts[CONF_DEVICES].setdefault(key, {})["tracked"] = True
            for key in user_input.get("ignore", []):
                if key not in user_input.get("track", []) and key not in self.opts[CONF_IGNORED]:
                    self.opts[CONF_IGNORED].append(key)
            return self._save()
        schema = vol.Schema(
            {
                vol.Optional("track", default=[]): _select(choices, multiple=True),
                vol.Optional("ignore", default=[]): _select(choices, multiple=True),
            }
        )
        return self.async_show_form(step_id="review_new", data_schema=schema)

    async def async_step_review_hubs(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        coord = self._coordinator()
        suggestions = coord.data.suggested_hubs if coord and coord.data else []
        if not suggestions:
            return self.async_abort(reason="no_suggestions")
        choices = [
            {"value": s["key"], "label": f"{s['name']} ({s.get('ip') or 'no IP'}): {', '.join(s.get('reasons') or [])}"}
            for s in suggestions
        ]
        if user_input is not None:
            confirm = user_input.get("confirm", [])
            for s in suggestions:
                if s["key"] in confirm:
                    try:
                        manage.add_hub(self.opts, s["name"], s.get("type") or "access_point", None, s.get("ip"), s["key"], source="manual")
                    except manage.ManageError:
                        continue
                    self.opts[CONF_DEVICES].pop(s["key"], None)
                elif s["key"] in user_input.get("dismiss", []):
                    manage.dismiss_hub(self.opts, s["key"])
            return self._save()
        schema = vol.Schema(
            {
                vol.Optional("confirm", default=[]): _select(choices, multiple=True),
                vol.Optional("dismiss", default=[]): _select(choices, multiple=True),
            }
        )
        return self.async_show_form(step_id="review_hubs", data_schema=schema)

    async def async_step_paste_list(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"result": ""}
        if user_input is not None:
            macs, ips = extract_addresses(user_input.get("text", ""))
            coord = self._coordinator()
            if not macs and not ips:
                errors["text"] = "no_addresses"
            elif coord is None:
                errors["base"] = "not_loaded"
            else:
                keys, unmatched = coord.match_addresses(macs, ips)
                for addr in list(unmatched):
                    if normalize_mac(addr):
                        keys.append(coord.seed_device(addr, None))
                if not keys:
                    errors["text"] = "no_matches"
                else:
                    manage.assign(self.opts, keys, user_input["hub"])
                    return self._save()
        schema = vol.Schema(
            {
                vol.Required("hub"): _select(self._hub_choices()),
                vol.Required("text"): sel.TextSelector(sel.TextSelectorConfig(multiline=True)),
            }
        )
        return self.async_show_form(step_id="paste_list", data_schema=schema, errors=errors, description_placeholders=placeholders)


def _clean_device(user_input: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"sleepy": bool(user_input.get("sleepy"))}
    if user_input.get("hub") and user_input["hub"] != AUTO_VALUE:
        out["hub"] = user_input["hub"]
    if user_input.get("name"):
        out["name"] = user_input["name"].strip()
    if user_input.get("icon"):
        out["icon"] = user_input["icon"]
    return out


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True

