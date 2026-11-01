"""Constants for NetOrbit."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "netorbit"
VERSION: Final = "0.2.8"

# Config entry data
CONF_SUBNET: Final = "subnet"
CONF_GATEWAY: Final = "gateway"

# Options
CONF_MODE: Final = "mode"
CONF_PING_INTERVAL: Final = "ping_interval"
CONF_DISCOVERY_INTERVAL: Final = "discovery_interval"
CONF_STALE_HOURS: Final = "stale_hours"
CONF_IGNORE_RANDOM_MAC: Final = "ignore_random_mac"
CONF_HUBS: Final = "hubs"
CONF_DEVICES: Final = "devices"
CONF_IGNORED: Final = "ignored"
CONF_AUTO_HUBS: Final = "auto_hubs"
CONF_DISMISSED_HUBS: Final = "dismissed_hubs"
CONF_SNMP_ENABLED: Final = "snmp_enabled"
CONF_SNMP_COMMUNITY: Final = "snmp_community"
CONF_GROUP_BY_TYPE: Final = "group_by_type"
CONF_CHECK_INTERNET: Final = "check_internet"
CONF_INCLUDE_OFF_SUBNET: Final = "include_off_subnet"

MODE_AUTO: Final = "auto"
MODE_MANUAL: Final = "manual"
MODES: Final = [MODE_AUTO, MODE_MANUAL]

DEFAULT_MODE: Final = MODE_AUTO
DEFAULT_PING_INTERVAL: Final = 60
DEFAULT_DISCOVERY_INTERVAL: Final = 300
DEFAULT_STALE_HOURS: Final = 24
DEFAULT_IGNORE_RANDOM_MAC: Final = True
DEFAULT_AUTO_HUBS: Final = True
DEFAULT_SNMP_ENABLED: Final = True
DEFAULT_INCLUDE_OFF_SUBNET: Final = False
# A controller client counts as present when its last_seen is no older than this.
OFF_SUBNET_PRESENT_S: Final = 300
SNMP_DEFAULT_COMMUNITY: Final = "public"
SNMP_PROBE_INTERVAL_S: Final = 3600

MIN_PING_INTERVAL: Final = 10
MAX_HOSTS: Final = 1024

# Hub types
HUB_ROUTER: Final = "router"
HUB_SWITCH: Final = "switch"
HUB_AP: Final = "access_point"
HUB_MESH: Final = "mesh_node"
HUB_OTHER: Final = "other"
HUB_TYPES: Final = [HUB_ROUTER, HUB_SWITCH, HUB_AP, HUB_MESH, HUB_OTHER]
HUB_ICONS: Final = {
    HUB_ROUTER: "mdi:router-network",
    HUB_SWITCH: "mdi:switch",
    HUB_AP: "mdi:access-point",
    HUB_MESH: "mdi:access-point-network",
    HUB_OTHER: "mdi:lan",
}
ROOT_HUB_ID: Final = "gateway"

# Where a device's hub placement came from
PLACE_MANUAL: Final = "manual"
PLACE_CONTROLLER: Final = "controller"
PLACE_SNMP: Final = "snmp"
PLACE_NONE: Final = "none"

# Status values
STATUS_ONLINE: Final = "online"
STATUS_RECENT: Final = "recent"
STATUS_STALE: Final = "stale"
STATUSES: Final = [STATUS_ONLINE, STATUS_RECENT, STATUS_STALE]

# Events
EVENT_NEW_DEVICE: Final = f"{DOMAIN}_new_device"
EVENT_DEVICE_STALE: Final = f"{DOMAIN}_device_stale"
EVENT_DEVICE_RECOVERED: Final = f"{DOMAIN}_device_recovered"
EVENT_INTERNET_DOWN: Final = f"{DOMAIN}_internet_down"
EVENT_INTERNET_UP: Final = f"{DOMAIN}_internet_up"

# Internet check: big public DNS resolvers that answer pings. Down means none answer for
# INTERNET_DOWN_AFTER ping rounds in a row while the router still answers.
INTERNET_KEY: Final = "internet"
INTERNET_TARGETS: Final = ("1.1.1.1", "8.8.8.8")
INTERNET_DOWN_AFTER: Final = 2

# Services
SERVICE_TRACK: Final = "track_device"
SERVICE_IGNORE: Final = "ignore_device"
SERVICE_SCAN: Final = "scan_now"
SERVICE_ASSIGN: Final = "assign_device"
SERVICE_ADD_HUB: Final = "add_hub"
SERVICE_REMOVE_HUB: Final = "remove_hub"
SERVICE_UPDATE_HUB: Final = "update_hub"
SERVICE_EXPORT: Final = "export_config"
SERVICE_IMPORT: Final = "import_config"
SERVICE_IMPORT_LIST: Final = "import_client_list"
SERVICE_CONFIRM_HUB: Final = "confirm_hub"
SERVICE_DISMISS_HUB: Final = "dismiss_hub"
SERVICE_RESET: Final = "reset_placement"

# Frontend
CARD_URL: Final = f"/{DOMAIN}/netorbit-card.js"
CARD_FILENAME: Final = "netorbit-card.js"

STORAGE_VERSION: Final = 1
