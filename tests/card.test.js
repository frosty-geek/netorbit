// Layout tests for the card. Run with: node --test tests/card.test.js
const test = require("node:test");
const assert = require("node:assert");
// Minimal browser stand-ins so the card file can load under Node.
globalThis.HTMLElement = class {};
globalThis.customElements = { get: () => undefined, define: () => {} };
globalThis.window = globalThis;
console.info = () => {};
const { netorbitLayout, NetOrbitCard } = require("../custom_components/netorbit/frontend/netorbit-card.js");

function model(hubDefs, devCounts) {
  const hubs = {};
  hubDefs.forEach(([key, parent]) => (hubs[key] = { key, parent, label: key }));
  const devices = [];
  Object.entries(devCounts).forEach(([hub, n]) => {
    for (let i = 0; i < n; i++) devices.push({ key: `${hub}-d${i}`, link: hub, label: `${hub} ${i}` });
  });
  return { hubs, devices, root: hubDefs[0][0] };
}

function dist(a, b) {
  return Math.hypot(a.x - b.x, a.y - b.y);
}

test("every device and hub gets a position", () => {
  const m = model([["r", null], ["sw", "r"], ["ap1", "sw"], ["ap2", "sw"], ["mesh", "r"]], { r: 2, sw: 3, ap1: 10, ap2: 7, mesh: 4 });
  const L = netorbitLayout(m);
  for (const d of m.devices) assert.ok(L.pos[d.key], d.key);
  for (const k of Object.keys(m.hubs)) assert.ok(L.pos[k], k);
});

test("devices do not overlap", () => {
  const m = model([["r", null], ["a", "r"], ["b", "r"]], { r: 5, a: 30, b: 25 });
  const L = netorbitLayout(m);
  const pts = m.devices.map((d) => L.pos[d.key]);
  for (let i = 0; i < pts.length; i++) for (let j = i + 1; j < pts.length; j++) assert.ok(dist(pts[i], pts[j]) > 30);
});

test("deeper hubs sit further out than their parents", () => {
  const m = model([["r", null], ["sw", "r"], ["ap", "sw"], ["rep", "ap"]], { rep: 3 });
  const L = netorbitLayout(m);
  const c = L.pos.r;
  assert.ok(dist(c, L.pos.ap) > dist(c, L.pos.sw));
  assert.ok(dist(c, L.pos.rep) > dist(c, L.pos.ap));
  assert.ok(dist(c, L.pos.rep) < 370);
});

test("flat network with only a router", () => {
  const m = model([["r", null]], { r: 12 });
  const L = netorbitLayout(m);
  assert.strictEqual(Object.keys(L.pos).length, 13);
});

test("unknown parent and cycles fall back to the root", () => {
  const m = model([["r", null], ["x", "missing"], ["y", "z"], ["z", "y"]], { x: 1, y: 1, z: 1 });
  m.devices.push({ key: "orphan", link: "nowhere", label: "orphan" });
  const L = netorbitLayout(m);
  for (const k of ["x", "y", "z", "orphan"]) assert.ok(L.pos[k], k);
});

test("large networks stagger device rings", () => {
  const m = model([["r", null], ["a", "r"]], { a: 120 });
  const L = netorbitLayout(m);
  const radii = new Set(m.devices.map((d) => Math.round(L.pos[d.key].r)));
  assert.strictEqual(radii.size, 2);
});

test("devices are grouped by type from their icon", () => {
  const { noGroupFor } = require("../custom_components/netorbit/frontend/netorbit-card.js");
  assert.strictEqual(noGroupFor("mdi:television"), "media");
  assert.strictEqual(noGroupFor("mdi:laptop"), "computers");
  assert.strictEqual(noGroupFor("mdi:cctv"), "cameras");
  assert.strictEqual(noGroupFor("mdi:thermostat"), "home");
  assert.strictEqual(noGroupFor("mdi:lan-connect"), "other");
});

test("height takes pixels or a CSS length, and nothing that could leave the style block", () => {
  const { noHeight } = require("../custom_components/netorbit/frontend/netorbit-card.js");
  assert.strictEqual(noHeight(1300), "1300px");
  assert.strictEqual(noHeight("1300"), "1300px");
  assert.strictEqual(noHeight(undefined), "900px");
  assert.strictEqual(noHeight(""), "900px");
  assert.strictEqual(noHeight(0), "900px");
  assert.strictEqual(noHeight("calc(100vh - 180px)"), "calc(100vh - 180px)");
  assert.strictEqual(noHeight("80vh"), "80vh");
  assert.strictEqual(noHeight("min(1200px, 90vh)"), "min(1200px, 90vh)");
  assert.strictEqual(noHeight("100px; } svg { display:none"), "900px");
  assert.strictEqual(noHeight("100px /*"), "900px");
  assert.strictEqual(noHeight("100px */"), "900px");
  assert.strictEqual(noHeight(true), "900px");
  assert.strictEqual(noHeight(false), "900px");
  assert.strictEqual(noHeight("1px</style><img src=x onerror=alert(1)>"), "900px");
  assert.strictEqual(noHeight("url('x')"), "900px");
});

const BASE = { x: -70, y: -40, w: 1140, h: 1090 };
const MIDDLE = { x: BASE.x + BASE.w / 2, y: BASE.y + BASE.h / 2 };
const near = (a, b) => Math.abs(a - b) < 1e-9;

test("zooming about the middle keeps the middle and the shape of the map", () => {
  const { netorbitZoomView } = require("../custom_components/netorbit/frontend/netorbit-card.js");
  const v = netorbitZoomView(null, MIDDLE, MIDDLE, 2);
  assert.ok(near(v.w, BASE.w / 2) && near(v.h, BASE.h / 2));
  assert.ok(near(v.x + v.w / 2, MIDDLE.x) && near(v.y + v.h / 2, MIDDLE.y));
  assert.ok(near(v.w / v.h, BASE.w / BASE.h));
});

test("the map point under the finger stays under it through a pinch", () => {
  const { netorbitZoomView } = require("../custom_components/netorbit/frontend/netorbit-card.js");
  const start = netorbitZoomView(null, MIDDLE, MIDDLE, 2);
  const anchor = { x: 300, y: 420 };
  const now = { x: 350, y: 470 }; // the fingers' midpoint moved, and they spread 1.5x
  const v = netorbitZoomView(start, anchor, now, 1.5);
  // Where `now` sits on the screen in the start view is where the anchor must sit in the new one.
  assert.ok(near((anchor.x - v.x) / v.w, (now.x - start.x) / start.w));
  assert.ok(near((anchor.y - v.y) / v.h, (now.y - start.y) / start.h));
  assert.ok(near(v.w, BASE.w / 3));
});

test("a drag pans by the finger's travel", () => {
  const { netorbitZoomView } = require("../custom_components/netorbit/frontend/netorbit-card.js");
  const v = netorbitZoomView(null, MIDDLE, MIDDLE, 2);
  const p = netorbitZoomView(v, { x: 400, y: 400 }, { x: 450, y: 430 }, 1);
  assert.ok(near(p.x, v.x - 50) && near(p.y, v.y - 30) && near(p.w, v.w));
});

test("zooming out past the whole map gives the whole map", () => {
  const { netorbitZoomView } = require("../custom_components/netorbit/frontend/netorbit-card.js");
  const v = netorbitZoomView(null, MIDDLE, MIDDLE, 2);
  assert.strictEqual(netorbitZoomView(v, MIDDLE, MIDDLE, 0.4), null);
  assert.strictEqual(netorbitZoomView(null, MIDDLE, MIDDLE, 0.5), null);
  assert.strictEqual(netorbitZoomView(null, MIDDLE, MIDDLE, 1), null);
});

test("zoom stops at the maximum and the view stays inside the map", () => {
  const { netorbitZoomView } = require("../custom_components/netorbit/frontend/netorbit-card.js");
  let v = null;
  for (let i = 0; i < 20; i++) v = netorbitZoomView(v, MIDDLE, MIDDLE, 2);
  assert.ok(near(v.w, BASE.w / 5) && near(v.h, BASE.h / 5));
  const corner = { x: BASE.x, y: BASE.y };
  const w = netorbitZoomView(null, corner, corner, 3);
  assert.ok(near(w.x, BASE.x) && near(w.y, BASE.y));
  // Dragging the finger far up and left pulls the far corner of the map into view and no further.
  const far = netorbitZoomView(w, corner, { x: corner.x - 5000, y: corner.y - 5000 }, 1);
  assert.ok(near(far.x + far.w, BASE.x + BASE.w) && near(far.y + far.h, BASE.y + BASE.h));
});

test("group hubs sit near their devices, not in the middle", () => {
  const hubs = { r: { key: "r", label: "Router" }, g: { key: "g", parent: "r", label: "TVs", kind: "group" } };
  const devices = [1, 2, 3].map((i) => ({ key: "d" + i, link: "g", label: "TV " + i }));
  const L = netorbitLayout({ hubs, devices, root: "r" });
  assert.ok(Math.hypot(L.pos.g.x - 500, L.pos.g.y - 500) > 200);
});

test("hubs keep clear of the internet node above the router", () => {
  const m = model([["r", null], ["a", "r"], ["b", "r"], ["c", "r"], ["d", "r"], ["e", "r"], ["f", "r"]], { a: 6, b: 6, c: 6, d: 6, e: 6, f: 6 });
  const L = netorbitLayout(m);
  const inet = { x: L.pos.r.x, y: L.pos.r.y - 62 }; // the default spot; the card moves it to a clear diagonal if a link runs there
  for (const k of "abcdef") assert.ok(dist(L.pos[k], inet) > 16 + 25, k); // node radii: internet 16, hub 25
});

// ---------------------------------------------------------------- scanning and sharing

const card = require("../custom_components/netorbit/frontend/netorbit-card.js");

test("scan mode comes from the summary sensor's attributes", () => {
  const { netorbitScanMode } = card;
  assert.strictEqual(netorbitScanMode({ scanning: true, first_scan_done: false, scan_phase: "ping" }), "takeover");
  // A first sweep that is not running (it failed, or has not started) never hides the map.
  assert.strictEqual(netorbitScanMode({ scanning: false, first_scan_done: false }), "none");
  assert.strictEqual(netorbitScanMode({ first_scan_done: false }), "none");
  assert.strictEqual(netorbitScanMode({ scanning: true, first_scan_done: true }), "chip");
  assert.strictEqual(netorbitScanMode({ scanning: false, first_scan_done: true }), "none");
  // Older versions of the integration have none of these: draw the map as before.
  assert.strictEqual(netorbitScanMode({ counts: {} }), "none");
  assert.strictEqual(netorbitScanMode(null), "none");
});

test("scan text shows the phase, the found count and the progress", () => {
  const { netorbitScanText } = card;
  const s = netorbitScanText({ scan_phase: "names", scan_progress: 50, found: 17 });
  assert.strictEqual(s.phase, "Asking devices their names");
  assert.strictEqual(s.line, "Scanning your network... found 17 devices");
  assert.strictEqual(s.pct, 50);
  assert.strictEqual(netorbitScanText({ scan_phase: "ping", found: 1 }).line, "Scanning your network... found 1 device");
  assert.strictEqual(netorbitScanText({ scan_phase: "arp" }).phase, "Reading the router's address table");
  assert.strictEqual(netorbitScanText({ scan_phase: "snmp" }).phase, "Asking switches what is plugged in where");
  assert.strictEqual(netorbitScanText({ scan_phase: "layout" }).phase, "Working out the layout");
  assert.strictEqual(netorbitScanText({ scan_phase: "starting" }).phase, "Getting ready");
  assert.strictEqual(netorbitScanText({ scan_phase: null, scan_progress: 140, found: -3 }).pct, 100);
  assert.strictEqual(netorbitScanText({ found: -3 }).found, 0);
});

// A map model shaped like the card builds it from Home Assistant, with everything a real one carries.
function richModel(order) {
  const hub = (key, label, hub_type, parent, icon) => ({ key, label, kind: "hub", hub_type, parent, link: parent, status: "online", icon,
    ip: "192.0.2." + (key.length + 1), mac: "00:00:5E:00:53:0" + key.length, entity: "sensor.netorbit_" + key, hostname: label.toLowerCase() + ".lan", vendor: "Acme" });
  const hubs = {
    "hub:gw": hub("hub:gw", "Kitchen Router", "router", null, "mdi:router-network"),
    "hub:sw": hub("hub:sw", "Basement Switch", "switch", "hub:gw", "mdi:switch"),
    "hub:ap": hub("hub:ap", "Attic AP", "access_point", "hub:sw", "mdi:access-point"),
    __new: { key: "__new", label: "New devices", kind: "newhub", parent: "hub:gw", status: "new", icon: "mdi:tag-plus-outline" },
  };
  let i = 10;
  const dev = (label, icon, link, extra) => Object.assign({ key: "00:00:5e:00:53:" + (i++).toString(16), label, kind: "device", link, parent: link, status: "online",
    icon, ip: "192.0.2." + i, mac: "00:00:5E:00:53:" + i.toString(16).toUpperCase(), entity: "sensor.netorbit_" + i, hostname: "host-" + i, vendor: "Acme", last_seen: "2026-01-01T00:00:00Z", placement: "snmp" }, extra || {});
  let devices = [
    dev("Living Room TV", "mdi:television", "hub:ap"),
    dev("Bedroom TV", "mdi:television", "hub:ap"),
    dev("Front Door Cam", "mdi:cctv", "hub:ap"),
    dev("Garage Cam", "mdi:cctv", "hub:sw"),
    dev("Office Laptop", "mdi:laptop", "hub:sw"),
    dev("192.0.2.77", "mdi:lan-connect", "hub:sw"),
    dev("Device 3A:91", "mdi:lan-connect", "hub:gw"),
    dev("Kids Tablet", "mdi:tablet", "hub:gw", { pending: true }),
    dev("esp-shed", "mdi:access-point-network", "hub:gw", { suggested: { key: "x", type: "access_point", reasons: ["Shed"] } }),
    { key: "new:new0", label: "Strange Gadget", kind: "new", link: "__new", status: "new", icon: "mdi:help-circle-outline", ip: "192.0.2.200", mac: "00:00:5E:00:53:C8", raw: { mac: "00:00:5E:00:53:C8", ip: "192.0.2.200" } },
  ];
  if (order) devices = order(devices);
  return { hubs, devices, root: "hub:gw", summary: { attributes: { config_entry_id: "abc" } }, internet: { key: "internet", entity: "sensor.netorbit_internet", kind: "internet", label: "Internet", status: "online" }, suggestions: { X: {} }, sig: "x" };
}
const REAL = ["Kitchen Router", "Basement Switch", "Attic AP", "Living Room TV", "Bedroom TV", "Front Door Cam", "Garage Cam", "Office Laptop", "Kids Tablet", "esp-shed", "Strange Gadget", "Acme", "host-", ".lan", "sensor.netorbit", "abc"];
const MAC_RE = /\b[0-9a-f]{2}([:-][0-9a-f]{2})+\b/i;

function assertSafe(text, what) {
  assert.ok(!text.includes("192.0.2."), `${what} holds an IP address`);
  assert.ok(!MAC_RE.test(text), `${what} holds a MAC address: ${(text.match(MAC_RE) || [])[0]}`);
  for (const name of REAL) assert.ok(!text.includes(name), `${what} holds ${name}`);
}

test("shareModel names devices by type, numbered per type by hub then name", () => {
  const m = card.shareModel(richModel());
  const byType = m.devices.map((d) => d.label);
  // Hubs by name (Attic AP, Basement Switch, Kitchen Router), then devices by name within each hub.
  assert.deepStrictEqual(byType, ["TV 1", "Camera 1", "TV 2", "Device 1", "Camera 2", "Laptop 1", "Device 2", "Tablet 1"]);
  assert.strictEqual(m.title, "My home network");
  // The same network in another order gives the same names.
  const again = card.shareModel(richModel((list) => list.slice().reverse()));
  assert.deepStrictEqual(again.devices.map((d) => d.label), byType);
  assert.deepStrictEqual(again.devices.map((d) => d.key), m.devices.map((d) => d.key));
});

test("shareModel hubs show their type only and waiting or suggested devices are left out", () => {
  const m = card.shareModel(richModel());
  assert.deepStrictEqual(Object.values(m.hubs).map((h) => h.label).sort(), ["Access point", "Router", "Switch"]);
  assert.ok(!Object.values(m.hubs).some((h) => h.kind === "newhub"));
  assert.ok(!m.devices.some((d) => d.kind === "new" || d.suggested || d.pending));
  assert.strictEqual(m.devices.length, 8);
  assert.deepStrictEqual(m.suggestions, {});
  assert.strictEqual(m.summary, null);
});

test("shared output never holds addresses or real names", () => {
  const m = card.shareModel(richModel());
  assertSafe(JSON.stringify(m), "shareModel");
  assertSafe(card.netorbitShareSvg(m, m.title), "share SVG");
  for (const d of m.devices) for (const k of ["ip", "mac", "entity", "hostname", "vendor", "raw", "last_seen"]) assert.strictEqual(d[k], undefined, k);
});

test("with names shown, labels stay but addresses still never do", () => {
  const m = card.shareModel(richModel(), { hideNames: false });
  const labels = m.devices.map((d) => d.label);
  assert.ok(labels.includes("Living Room TV") && labels.includes("Garage Cam"));
  assert.ok(Object.values(m.hubs).some((h) => h.label === "Attic AP"));
  // A device named after its address gets a generic name.
  assert.ok(!labels.some((l) => l.includes("192.0.2.") || MAC_RE.test(l)), labels.join(","));
  const svg = card.netorbitShareSvg(m, "Network");
  assert.ok(!svg.includes("192.0.2.") && !MAC_RE.test(svg));
  assert.ok(svg.includes("Living Room TV"));
});

test("the reveal pops nodes in the order the beam passes them", () => {
  const { netorbitRevealNode, netorbitRevealGlobal } = card;
  const top = netorbitRevealNode(0.4, 0.1), left = netorbitRevealNode(0.4, 1.5 * Math.PI);
  assert.ok(top.o > 0 && left.o === 0);
  assert.ok(netorbitRevealNode(0.4, 0.1).at < netorbitRevealNode(0.4, Math.PI).at);
  for (const a of [0, 1, 3, 6.2]) assert.deepStrictEqual([netorbitRevealNode(4, a).s, netorbitRevealNode(4, a).o, netorbitRevealNode(4, a).draw], [1, 1, 1]);
  // Reduced motion: no scaling, only a fade.
  assert.strictEqual(netorbitRevealNode(0.4, 0.1, true).s, 1);
  assert.strictEqual(netorbitRevealGlobal(0.5).labels, 0);
  assert.strictEqual(netorbitRevealGlobal(4).labels, 1);
  assert.strictEqual(netorbitRevealGlobal(0.5).header, false);
  assert.strictEqual(netorbitRevealGlobal(4).header, true);
});

test("links draw in from their start", () => {
  const { noPartialPath } = card;
  assert.strictEqual(noPartialPath("M0,0 L10,20", 0.5), "M0.0,0.0 L5.0,10.0");
  assert.strictEqual(noPartialPath("M0,0 Q10,0 10,10", 1), "M0,0 Q10,0 10,10");
  assert.strictEqual(noPartialPath("M0,0 Q10,0 10,10", 0.5), "M0.0,0.0 Q5.0,0.0 7.5,2.5");
});

// Names shown, one device called `name`: what does the share model make of it?
function sharedLabel(name) {
  const m = richModel();
  m.devices = [Object.assign({}, m.devices[0], { label: name })];
  return card.shareModel(m, { hideNames: false }).devices[0].label;
}

test("with names shown, a name holding any form of address is replaced", () => {
  const leaky = [
    "192.0.2.7", "Cam 192.0.2.7", "Cam_192.0.2.7", "cam-192.0.2.7-lan", // IPv4, also next to an underscore
    "00:00:5e:00:53:01", "00-00-5E-00-53-01", "Device 53:01", // MAC with separators, whole or partial
    "00005e005301", "Cam 00005E005301", "Cam_00005e005301", // 12 hex characters
    "0000.5e00.5301", "Cam_0000.5e00.5301", // dotted Cisco form
    "ESP_A1B2C3", "esp32-a1b2c3", "Plug 00ff12", "Shelly.5e0053", // last six characters of a MAC after a separator
    "2001:db8::1", "Cam_2001:db8::7", "fe80::", "::1", "2001:db8:0:0:0:0:0:1", // IPv6
  ];
  for (const n of leaky) assert.notStrictEqual(sharedLabel(n), n, n + " should be replaced");
});

test("with names shown, ordinary names stay", () => {
  // Names made of the letters a to f only (Deadbeef, Facade) are words, not addresses: six hex characters need a digit.
  // A name needs more than a short number or a version to be treated as an address.
  const fine = ["Living Room TV", "Office Switch", "Cafe", "Deadbeef Lamp", "Facade Light", "Decade", "Echo Dot 3", "Pixel 7 Pro", "Room 12", "Hallway 2nd Floor", "TV: Den", "Attic AP2"];
  for (const n of fine) assert.strictEqual(sharedLabel(n), n, n + " should stay");
});

test("hover details show the network and VLAN the controller reports, and the port", () => {
  const hubs = { "hub:sw": { kind: "hub", label: "Switch" } };
  const tip = (n) => NetOrbitCard.prototype._tipHtml.call({ _dhcpTip: () => "" }, { kind: "device", label: "Plug", status: "online", link: "hub:sw", ...n }, hubs, () => "#fff");
  const html = tip({ network: "IoT", vlan: 15, port: 7 });
  assert.match(html, /Network<\/span>IoT · VLAN 15/);
  assert.match(html, /port 7/);
  assert.doesNotMatch(tip({}), /Network/);
  assert.match(tip({ vlan: 0 }), /VLAN 0/);
});
