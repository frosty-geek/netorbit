"""Generate fake home networks with a known true layout.

Each network is a tree: the router at the top, then switches (managed with SNMP, or
unmanaged with no IP), access points and mesh nodes, and devices hanging off any of
them. From the tree we derive exactly what NetOrbit would observe: who answers ping,
the ARP table, reverse DNS names, maker prefixes, SNMP answers and each managed
switch's MAC table (port toward every MAC). The tree itself is the answer key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import random

# Maker prefixes. First bytes are even with the second hex digit not 2/6/A/E, so none look randomized.
VENDORS = {
    "ubiquiti": ("10:00:5E", "Ubiquiti Inc"),
    "eero": ("14:00:5E", "eero inc"),
    "tplink": ("18:00:5E", "TP-LINK TECHNOLOGIES CO.,LTD."),
    "netgear": ("1C:00:5E", "NETGEAR"),
    "snapav": ("30:00:5E", "SnapAV"),
    "apple": ("34:00:5E", "Apple, Inc."),
    "samsung": ("38:00:5E", "Samsung Electronics Co.,Ltd"),
    "espressif": ("3C:00:5E", "Espressif Inc."),
    "sonos": ("40:00:5E", "Sonos, Inc."),
    "google": ("44:00:5E", "Google, Inc."),
    "roku": ("48:00:5E", "Roku, Inc"),
    "generic": ("4C:00:5E", "Generic Devices Ltd"),
    "unknown": ("50:00:5E", None),
}

DEVICE_KINDS = [
    # (hostname pattern, vendor, wired probability)
    ("living-room-tv", "samsung", 0.3), ("bedroom-tv", "samsung", 0.1), ("roku-{n}", "roku", 0.1),
    ("kitchen-speaker", "sonos", 0.2), ("office-speaker", "sonos", 0.5), ("nas", "generic", 1.0),
    ("desktop-{n}", "generic", 0.8), ("printer", "generic", 0.3), ("thermostat-{n}", "google", 0.0),
    ("cam-{n}", "generic", 0.2), ("plug-{n}", "tplink", 0.0), ("esp-{n}", "espressif", 0.0),
    ("bulb-{n}", "espressif", 0.0), ("ipad-{n}", "apple", 0.0), ("apple-tv-{n}", "apple", 0.4),
    ("", "unknown", 0.2), ("", "generic", 0.3), ("Kühlschrank", "generic", 0.0), ("tv", "samsung", 0.2),
]


@dataclass
class Node:
    key: str
    kind: str  # router | switch_managed | switch_unmanaged | ap_unifi | ap_generic | mesh | device
    mac: str | None
    ip: str | None
    parent: str | None
    port_on_parent: int | None = None  # port on the parent switch (if the parent keeps a table)
    hostname: str | None = None
    vendor: str | None = None
    answers_ping: bool = True
    random_mac: bool = False
    wireless: bool = False
    snmp_community: str | None = None  # managed switches (and some routers)
    ports_used: int = 1


@dataclass
class Network:
    name: str
    subnet: str
    gateway: str
    nodes: dict[str, Node] = field(default_factory=dict)
    community: str = "public"
    drop_rate: float = 0.0  # fraction of MAC table entries a switch has aged out
    controller: str | None = None  # "unifi" (trackers report the AP) or "unifi_api" (HA unifi entry with uplinks)

    # ----- answer key helpers ---------------------------------------------------

    def path_to_root(self, key: str) -> list[str]:
        out = []
        cur: str | None = key
        while cur:
            out.append(cur)
            cur = self.nodes[cur].parent
        return out

    def hubs(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind != "device"]

    def devices(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.kind == "device"]

    def mac_tables(self, rng: random.Random) -> dict[str, dict[str, int]]:
        """What every managed switch (and SNMP router) would report: {switch ip: {mac: port}}."""
        tables: dict[str, dict[str, int]] = {}
        for sw in self.nodes.values():
            if not sw.snmp_community or not sw.ip:
                continue
            table: dict[str, int] = {}
            sw_path = set(self.path_to_root(sw.key))
            for n in self.nodes.values():
                if n.key == sw.key or not n.mac:
                    continue
                path = self.path_to_root(n.key)
                if sw.key in path:
                    # Below this switch: the port of the child on the way down.
                    child = path[path.index(sw.key) - 1]
                    port = self.nodes[child].port_on_parent
                else:
                    port = 1 if sw.kind != "router" else None  # uplink is port 1
                    if port is None:
                        continue
                if port is None:
                    continue
                if rng.random() < self.drop_rate and n.kind == "device":
                    continue
                table[n.mac] = port
            _ = sw_path
            tables[sw.ip] = table
        return tables


class Builder:
    """Build a network step by step with consistent addresses."""

    def __init__(self, name: str, rng: random.Random, subnet: str = "192.0.2.0/24", gateway_last: int = 1) -> None:
        self.rng = rng
        net = ipaddress.ip_network(subnet)
        self.hosts = [str(h) for h in net.hosts()]
        gw = self.hosts[gateway_last - 1]
        self.net = Network(name=name, subnet=subnet, gateway=gw)
        self._ip_pool = [h for h in self.hosts if h != gw]
        rng.shuffle(self._ip_pool)
        self._mac_counter: dict[str, int] = {}
        self._names: dict[str, int] = {}
        self.add("router", "router", None, ip=gw, vendor="netgear", hostname="router")

    def mac(self, vendor: str, random_mac: bool = False) -> str:
        if random_mac:
            b = [self.rng.randrange(256) for _ in range(6)]
            b[0] = (b[0] & 0xFC) | 0x02
            return ":".join(f"{x:02x}" for x in b)
        prefix = VENDORS[vendor][0].lower()
        n = self._mac_counter.get(prefix, 0) + 1
        self._mac_counter[prefix] = n
        return f"{prefix}:{(n >> 16) & 0xFF:02x}:{(n >> 8) & 0xFF:02x}:{n & 0xFF:02x}"

    def ip(self) -> str:
        return self._ip_pool.pop()

    def add(self, key, kind, parent, ip=None, vendor="generic", hostname=None, mac=None, **kw) -> Node:
        if key in self.net.nodes:
            raise ValueError(key)
        port = None
        if parent:
            p = self.net.nodes[parent]
            if p.snmp_community or p.kind in ("switch_managed", "router"):
                p.ports_used += 1
                port = p.ports_used
        node = Node(
            key=key, kind=kind, mac=mac or self.mac(vendor, kw.pop("random_mac", False)),
            ip=ip, parent=parent, port_on_parent=port, hostname=hostname, vendor=vendor, **kw,
        )
        node.random_mac = node.mac[1] in "26ae"
        self.net.nodes[key] = node
        return node

    def device(self, parent: str, wireless: bool | None = None, **kw) -> Node:
        pattern, vendor, p_wired = self.rng.choice(DEVICE_KINDS)
        n = self._names.get(pattern, 0) + 1
        self._names[pattern] = n
        host = pattern.replace("{n}", str(n)) if pattern else None
        if host and "{n}" not in pattern and n > 1:
            host = f"{host}-{n}"  # duplicate names get a suffix, like real DHCP
        key = f"dev{len(self.net.nodes)}"
        return self.add(key, "device", parent, ip=self.ip(), vendor=vendor, hostname=host,
                        wireless=wireless if wireless is not None else self.rng.random() > p_wired, **kw)


# ----- named shapes -----------------------------------------------------------------

def flat(rng: random.Random, n: int = 12) -> Network:
    b = Builder("flat", rng)
    for _ in range(n):
        b.device("router")
    return b.net


def mesh_no_data(rng: random.Random, nodes: int = 3, n: int = 20) -> Network:
    b = Builder("mesh_no_data", rng, subnet="192.0.2.0/24" if n < 200 else "10.20.0.0/22")  # example-ok: synthetic test value
    b.net.nodes["router"].vendor = "eero"
    b.net.nodes["router"].mac = b.mac("eero")
    for i in range(nodes - 1):
        b.add(f"mesh{i}", "mesh", "router", ip=b.ip(), vendor="eero", hostname=None)
    hubs = ["router"] + [f"mesh{i}" for i in range(nodes - 1)]
    for _ in range(n):
        b.device(rng.choice(hubs), wireless=True)
    return b.net


def unifi(rng: random.Random, aps: int = 3, n: int = 25) -> Network:
    b = Builder("unifi", rng)
    b.net.controller = "unifi"
    b.add("usw", "switch_unmanaged", "router", ip=b.ip(), vendor="ubiquiti", hostname="usw-lite-8")
    for i in range(aps):
        b.add(f"ap{i}", "ap_unifi", "usw", ip=b.ip(), vendor="ubiquiti", hostname=f"u6-lite-{i}")
    for _ in range(n):
        b.device(f"ap{rng.randrange(aps)}", wireless=True)
    for _ in range(3):
        b.device("usw", wireless=False)
    return b.net


def unifi_flex(rng: random.Random, n: int = 18) -> Network:
    """USW Flex switches that have no SNMP, one behind the other, an AP behind the second.

    Only the controller (the HA unifi integration) knows who sits where: wired clients report
    their switch and port, Wi-Fi clients their AP, and every UniFi device its uplink.
    """
    b = Builder("unifi_flex", rng)
    b.net.controller = "unifi_api"
    b.net.nodes["router"].vendor = "ubiquiti"
    b.net.nodes["router"].mac = b.mac("ubiquiti")
    b.add("swA", "switch_unmanaged", "router", ip=b.ip(), vendor="ubiquiti", hostname="usw-flex-a")
    b.add("swB", "switch_unmanaged", "swA", ip=b.ip(), vendor="ubiquiti", hostname="usw-flex-b")
    b.add("ap", "ap_unifi", "swB", ip=b.ip(), vendor="ubiquiti", hostname="u7-lite")
    ports: dict[str, int] = {}
    for key, node in b.net.nodes.items():
        if node.parent:
            ports[node.parent] = ports.get(node.parent, 0) + 1
            node.port_on_parent = ports[node.parent] + 1
    for i in range(n):
        parent = ["router", "swA", "swB", "ap", "ap"][i % 5]
        d = b.device(parent, wireless=parent == "ap")
        if parent != "ap":
            ports[parent] = ports.get(parent, 0) + 1
            d.port_on_parent = ports[parent] + 1
    return b.net


def snmp_single(rng: random.Random, aps: int = 2, n: int = 20, community: str = "public") -> Network:
    b = Builder("snmp_single", rng)
    b.net.community = community
    b.add("sw", "switch_managed", "router", ip=b.ip(), vendor="snapav", hostname="poe-switch", snmp_community=community)
    for i in range(aps):
        b.add(f"ap{i}", "ap_generic", "sw", ip=b.ip(), vendor="snapav", hostname=f"ap-{i}")
    hubs = ["sw"] + [f"ap{i}" for i in range(aps)]
    for _ in range(n):
        b.device(rng.choice(hubs))
    return b.net


def snmp_chain(rng: random.Random, depth: int = 3, n: int = 30, community: str = "public") -> Network:
    """Router > sw0 > sw1 > sw2 ..., APs and an unmanaged switch hanging off the chain."""
    b = Builder("snmp_chain", rng, subnet="192.0.2.0/24" if n < 200 else "10.20.0.0/22")  # example-ok: synthetic test value
    b.net.community = community
    parent = "router"
    for i in range(depth):
        b.add(f"sw{i}", "switch_managed", parent, ip=b.ip(), vendor="netgear", hostname=f"gs308-{i}", snmp_community=community)
        parent = f"sw{i}"
    b.add("ap_up", "ap_generic", "sw0", ip=b.ip(), vendor="ubiquiti", hostname="uap-upstairs")
    b.add("ap_down", "ap_generic", f"sw{depth - 1}", ip=b.ip(), vendor="ubiquiti", hostname="uap-downstairs")
    b.add("dumb", "switch_unmanaged", "sw1" if depth > 1 else "sw0", ip=None, mac=None, vendor="generic")
    b.net.nodes["dumb"].mac = None
    b.add("ap_shed", "ap_generic", "dumb", ip=b.ip(), vendor="netgear", hostname="shed-extender")
    hubs = ["router"] + [f"sw{i}" for i in range(depth)] + ["ap_up", "ap_down", "dumb", "ap_shed"]
    for _ in range(n):
        b.device(rng.choice(hubs))
    return b.net


def snmp_router(rng: random.Random, n: int = 15, community: str = "public") -> Network:
    """The router itself answers SNMP and keeps the table (common on prosumer gateways)."""
    b = Builder("snmp_router", rng, gateway_last=254)
    b.net.community = community
    r = b.net.nodes["router"]
    r.snmp_community = community
    r.vendor = "ubiquiti"
    r.mac = b.mac("ubiquiti")
    b.add("ap", "ap_generic", "router", ip=b.ip(), vendor="ubiquiti", hostname="u7-pro")
    for _ in range(n):
        b.device(rng.choice(["router", "ap"]))
    return b.net


def random_network(seed: int) -> Network:
    """Anything goes: random shape, sizes, quirks."""
    rng = random.Random(seed)
    big = rng.random() < 0.08
    subnet = "10.20.0.0/22" if big else "192.0.2.0/24"  # example-ok: synthetic test value
    b = Builder(f"random{seed}", rng, subnet=subnet, gateway_last=rng.choice([1, 1, 1, 2, 254]))
    community = rng.choice(["public", "public", "s3cretRO"])
    b.net.community = community
    b.net.drop_rate = rng.choice([0.0, 0.0, 0.05, 0.2])
    if rng.random() < 0.3:
        b.net.controller = "unifi"
    hubs = ["router"]
    switches = ["router"] if rng.random() < 0.15 else []
    if switches:
        b.net.nodes["router"].snmp_community = community
    for i in range(rng.randrange(0, 4)):
        parent = rng.choice(hubs)
        managed = rng.random() < 0.6
        kind = "switch_managed" if managed else "switch_unmanaged"
        node = b.add(f"sw{i}", kind, parent, ip=b.ip() if managed or rng.random() < 0.5 else None,
                     vendor=rng.choice(["netgear", "tplink", "snapav", "ubiquiti"]), hostname=rng.choice([f"switch-{i}", None, f"usw-{i}"]),
                     snmp_community=community if managed and rng.random() < 0.8 else None)
        if node.ip is None and kind == "switch_unmanaged":
            node.mac = None
        hubs.append(node.key)
        if node.snmp_community:
            switches.append(node.key)
    for i in range(rng.randrange(0, 5)):
        parent = rng.choice(hubs)
        if b.net.nodes[parent].kind == "switch_unmanaged" and b.net.nodes[parent].mac is None and rng.random() < 0.5:
            parent = "router"
        kind = rng.choice(["ap_unifi", "ap_generic", "mesh"])
        vendor = {"ap_unifi": "ubiquiti", "mesh": rng.choice(["eero", "tplink"]), "ap_generic": rng.choice(["snapav", "tplink", "netgear", "generic"])}[kind]
        host = {"ap_unifi": f"u6-{i}", "mesh": rng.choice([f"deco-{i}", None]), "ap_generic": rng.choice([f"ap-{i}", f"eap{i}", None])}[kind]
        b.add(f"ap{i}", kind, parent, ip=b.ip(), vendor=vendor, hostname=host)
        hubs.append(f"ap{i}")
    n_dev = rng.randrange(3, 60) if not big else rng.randrange(300, 700)
    for _ in range(n_dev):
        d = b.device(rng.choice(hubs))
        d.answers_ping = rng.random() > 0.08
        if rng.random() < 0.12:
            d.mac = b.mac("apple", random_mac=True)
            d.random_mac = True
    return b.net


SHAPES = {
    "flat_tiny": lambda rng: flat(rng, 3),
    "flat": lambda rng: flat(rng, 15),
    "mesh_no_data": mesh_no_data,
    "unifi": unifi,
    "unifi_flex": unifi_flex,
    "snmp_single": snmp_single,
    "snmp_single_custom_community": lambda rng: snmp_single(rng, community="s3cretRO"),
    "snmp_chain": snmp_chain,
    "snmp_router": snmp_router,
}
