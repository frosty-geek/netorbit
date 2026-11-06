/* NetOrbit card for Home Assistant
 * Draws a live radial map from the NetOrbit integration's sensors.
 * https://github.com/the-scott-davis/netorbit  (MIT)
 */
const NETORBIT_VERSION = "0.2.8-frosty.1";

const NO_STATUS = {
  online: { color: "#34d399", label: "Online" },
  recent: { color: "#fbbf24", label: "Seen recently" },
  stale: { color: "#f87171", label: "Not seen" },
  new: { color: "#c084fc", label: "New" },
  unknown: { color: "#64748b", label: "Unknown" },
};

// Icon name -> SVG path data, shared by every card on the page (see _resolveIcons).
const NO_ICON_PATHS = new Map();
const NO_ICON_PENDING = new Set();
// Cards waiting on icons. Any card can start a lookup, so a finished lookup fills every card, not only the one
// that asked (a second card, or the card editor's preview, would otherwise keep blank icons).
const NO_ICON_CARDS = new Set();

// mdi:pin, drawn on pinned devices in edit mode.
const NO_PIN_PATH = "M16,12V4H17V2H7V4H8V12L6,14V16H11.2V22H12.8V16H18V14L16,12Z";
// mdi:orbit, the NetOrbit mark on shared images and videos.
const NO_ORBIT_PATH = "M8.11,1.75C9.3,1.25 10.62,1 12,1C18.08,1 23,5.92 23,12C23,18.08 18.08,23 12,23C5.92,23 1,18.08 1,12C1,10.62 1.25,9.3 1.72,8.08C2.24,8.61 2.83,8.96 3.45,9.18C3.16,10.07 3,11 3,12A9,9 0 0,0 12,21A9,9 0 0,0 21,12A9,9 0 0,0 12,3C11,3 10.07,3.16 9.18,3.45C8.96,2.83 8.61,2.24 8.11,1.75M4.93,2.93C6.03,2.93 6.93,3.82 6.93,4.93A2,2 0 0,1 4.93,6.93C3.82,6.93 2.93,6.03 2.93,4.93C2.93,3.82 3.82,2.93 4.93,2.93M12,7A5,5 0 0,1 17,12A5,5 0 0,1 12,17A5,5 0 0,1 7,12A5,5 0 0,1 12,7Z";

// Possible hubs get a color of their own, so they never look like "seen recently".
const NO_SUGGEST = { color: "#f472b6", light: "#f9a8d4", soft: "rgba(244,114,182,.5)" };

const NO_HUB_TYPE = {
  router: "Router",
  switch: "Switch",
  access_point: "Access point",
  mesh_node: "Mesh node",
  other: "Hub",
};

function noEsc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function noRel(iso) {
  if (!iso) return "never";
  const t = new Date(iso).getTime();
  if (isNaN(t)) return "never";
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " d ago";
}

function noTrunc(s, n) {
  s = String(s || "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

/** The map's max height: pixels, or a CSS length like "calc(100vh - 180px)". Anything else is 900px.
 * It lands in a <style> block, so only characters a length needs get through (no ; : { } < > or quotes). */
function noHeight(v) {
  if (typeof v === "boolean") return "900px";
  const n = Number(v);
  if (v !== "" && v != null && Number.isFinite(n)) return n > 0 ? `${n}px` : "900px";
  const s = String(v == null ? "" : v).trim();
  if (!/^[a-z0-9\s.,%()+*/-]+$/i.test(s)) return "900px";
  if (s.indexOf("/*") !== -1 || s.indexOf("*/") !== -1) return "900px";
  if (typeof CSS !== "undefined" && CSS.supports && !CSS.supports("max-height", s)) return "900px";
  return s;
}

/** Pure layout: returns positions for hubs and leaves. Exported for tests. */
function netorbitLayout(model, opts) {
  const o = Object.assign({ cx: 500, cy: 500, rDev: 370, rHub1: 135, hubStep: 125, rHubMax: 290, rGroup: 245 }, opts || {});
  const { hubs, root, devices } = model;
  const kidsHub = {};
  const kidsDev = {};
  Object.keys(hubs).forEach((k) => { kidsHub[k] = []; kidsDev[k] = []; });
  Object.values(hubs).forEach((h) => {
    if (h.key === root) return;
    const p = hubs[h.parent] ? h.parent : root;
    kidsHub[p].push(h.key);
  });
  devices.forEach((d) => {
    const p = hubs[d.link] ? d.link : root;
    kidsDev[p].push(d);
  });
  // By label, or by "sort" where a node has one (a shared map keeps the card's order with generic names).
  const lab = (n) => String(n.sort != null ? n.sort : n.label);
  const byLabel = (a, b) => lab(a).localeCompare(lab(b));
  Object.values(kidsDev).forEach((a) => a.sort(byLabel));
  Object.values(kidsHub).forEach((a) => a.sort((x, y) => {
    // keep the "new devices" bubble last
    if (x === "__new") return 1;
    if (y === "__new") return -1;
    return lab(hubs[x]).localeCompare(lab(hubs[y]));
  }));

  // DFS producing slots
  const slots = []; // {type:'dev', d, hub} or {type:'gap', hub}
  const range = {};
  const depth = {};
  const guard = new Set();
  const walk = (h, dep) => {
    if (guard.has(h)) return;
    guard.add(h);
    depth[h] = dep;
    const start = slots.length;
    kidsDev[h].forEach((d) => slots.push({ type: "dev", d, hub: h }));
    kidsHub[h].forEach((c) => walk(c, dep + 1));
    if (slots.length === start && h !== root) slots.push({ type: "gap", hub: h });
    range[h] = [start, slots.length - 1];
  };
  walk(root, 0);
  // orphans (cycles) hang off root
  Object.keys(hubs).forEach((h) => { if (!guard.has(h)) { kidsHub[root].push(h); walk(h, 1); } });

  const N = Math.max(slots.length, 1);
  const step = (Math.PI * 2) / N;
  const start = -Math.PI / 2 - (N > 1 ? step * (range[root] ? 0 : 0) : 0);
  const angle = (i) => start + step * i + step / 2;
  const pos = {};
  pos[root] = { x: o.cx, y: o.cy, a: -Math.PI / 2, depth: 0 };
  const stagger = N > 64;
  slots.forEach((s, i) => {
    if (s.type !== "dev") return;
    const a = angle(i);
    const r = o.rDev - (stagger && i % 2 ? 36 : 0);
    pos[s.d.key] = { x: o.cx + r * Math.cos(a), y: o.cy + r * Math.sin(a), a, r, hub: s.hub };
  });
  const maxDepth = Math.max(1, ...Object.keys(depth).map((k) => depth[k]));
  const hubStep = Math.min(o.hubStep, (o.rHubMax - o.rHub1) / Math.max(1, maxDepth - 1));
  Object.keys(hubs).forEach((h) => {
    if (h === root || !range[h]) return;
    const [a0, a1] = range[h];
    const a = (angle(a0) + angle(a1)) / 2;
    const r = hubs[h].kind === "group" ? o.rGroup : Math.min(o.rHubMax, o.rHub1 + (depth[h] - 1) * hubStep);
    pos[h] = { x: o.cx + r * Math.cos(a), y: o.cy + r * Math.sin(a), a, r, depth: depth[h] };
  });
  netorbitSpreadHubs(pos, Object.keys(hubs).filter((h) => h !== root && pos[h]), o);
  return { pos, slots, N, arc: (Math.PI * 2 * o.rDev) / N, kidsHub, depth };
}

/** Push apart hubs that ended up on top of each other, keeping them between the router and the device ring. */
function netorbitSpreadHubs(pos, keys, o) {
  const minD = 88;
  const rMin = 105, rMax = o.rDev - 60;
  for (let iter = 0; iter < 120; iter++) {
    let moved = false;
    for (let i = 0; i < keys.length; i++) {
      for (let j = i + 1; j < keys.length; j++) {
        const p = pos[keys[i]], q = pos[keys[j]];
        const d = Math.hypot(p.x - q.x, p.y - q.y);
        if (d >= minD) continue;
        moved = true;
        if (Math.abs(p.r - q.r) < 30) {
          // Same ring: open up the angle between them.
          let da = q.a - p.a;
          while (da > Math.PI) da -= 2 * Math.PI;
          while (da < -Math.PI) da += 2 * Math.PI;
          const need = (minD - d) / Math.max(p.r, 60) / 2 + 0.01;
          const s = da >= 0 ? 1 : -1;
          p.a -= s * need; q.a += s * need;
          // On a crowded ring, also step one of them outward.
          if (iter > 40) q.r = Math.min(rMax, q.r + 6);
        } else {
          const s = p.r < q.r ? -1 : 1;
          p.r = Math.max(rMin, Math.min(rMax, p.r + s * 4));
          q.r = Math.max(rMin, Math.min(rMax, q.r - s * 4));
        }
        for (const z of [p, q]) { z.x = o.cx + z.r * Math.cos(z.a); z.y = o.cy + z.r * Math.sin(z.a); }
      }
    }
    if (!moved) break;
  }
}

const NO_PLACEMENT = {
  manual: "You placed it",
  controller: "Reported by your Wi-Fi system",
  snmp: "Seen in a switch's MAC table",
  none: "Not known yet",
};

// Groups used when nothing reports which hub a device uses.
const NO_GROUPS = [
  { id: "media", label: "TV & media", icon: "mdi:television-classic", match: /television|speaker|soundbar|gamepad|nintendo|playstation|xbox|virtual-reality|apple|cast|music|movie|play/ },
  { id: "computers", label: "Computers & phones", icon: "mdi:laptop", match: /laptop|monitor|desktop|cellphone|tablet|watch|server|printer|nas|book/ },
  { id: "cameras", label: "Cameras & security", icon: "mdi:cctv", match: /cctv|camera|doorbell|smoke|alarm|lock|shield/ },
  { id: "home", label: "Smart home", icon: "mdi:home-automation", match: /chip|thermostat|robot|garage|light|bulb|power|socket|plug|home|fan|purifier|kettle|fridge|sprinkler|pool|ev-|car|bowl|cat|dog|weather|blind|switch/ },
];

function noGroupFor(icon) {
  const s = String(icon || "");
  for (const g of NO_GROUPS) if (g.match.test(s)) return g.id;
  return "other";
}

/** Builds the map's SVG markup for a model. Pure apart from reading cached icon paths, so the card, the share
 * image and the share video all draw the same map. Besides the markup it returns a plain description of what
 * was drawn (scene), which the share video paints on a canvas frame by frame. */
function netorbitDraw(model, cfg, opts) {
  cfg = cfg || {};
  opts = opts || {};
  const { hubs, devices, root } = model;
  const L = netorbitLayout(model, {});
  const pos = L.pos;
  const C = pos[root];
  const col = (s) => (s === "group" ? "#64748b" : (NO_STATUS[s] || NO_STATUS.unknown).color);
  const live = (s) => s === "online";
  const fs = Math.max(9, Math.min(12.5, L.arc * 0.42));
  const maxChars = L.N > 60 ? 16 : 22;
  const edit = !!opts.edit;
  const scene = { C, links: [], nodes: [], hubLabels: [], devLabels: [] };

  const curve = (p, q, bend) => {
    const mx = (p.x + q.x) / 2, my = (p.y + q.y) / 2;
    const k = bend == null ? 0.18 : bend;
    const cx = mx + (C.x - mx) * k, cy = my + (C.y - my) * k;
    return `M${p.x.toFixed(1)},${p.y.toFixed(1)} Q${cx.toFixed(1)},${cy.toFixed(1)} ${q.x.toFixed(1)},${q.y.toFixed(1)}`;
  };
  let links = "";
  const link = (k, cls, d, color) => {
    links += `<path class="lk ${cls}" data-k="${noEsc(k)}" d="${d}" stroke="${color}"/>`;
    scene.links.push({ k, cls, d, color });
  };
  Object.values(hubs).forEach((h) => {
    if (h.key === root || !pos[h.key]) return;
    const p = pos[hubs[h.parent] ? h.parent : root];
    const s = h.kind === "newhub" ? "new" : h.status;
    const faint = h.kind === "group" ? "faint" : "strong";
    link(h.key, `${faint} ${live(s) ? "live" : ""} ${h.kind === "newhub" ? "dashed" : ""}`, curve(p, pos[h.key], 0), col(s));
  });
  // The internet sits just above the router: green when public DNS servers answer, red when they
  // don't but the router does, grey when NetOrbit can't tell.
  const inet = model.internet;
  if (inet) {
    // Straight up, unless a link from the router already runs there; then the nearest clear diagonal.
    const out = Object.values(hubs).filter((h) => h.key !== root && pos[h.key] && (hubs[h.parent] ? h.parent : root) === root)
      .map((h) => Math.atan2(pos[h.key].y - C.y, pos[h.key].x - C.x));
    const gap = (a) => Math.min(Math.PI, ...out.map((b) => Math.abs(Math.atan2(Math.sin(a - b), Math.cos(a - b)))));
    const clear = [-90, -125, -55].map((d) => (d * Math.PI) / 180).find((x) => gap(x) > 0.35);
    const a = clear === undefined ? -Math.PI / 2 : clear;
    pos.internet = { x: +(C.x + 62 * Math.cos(a)).toFixed(1), y: +(C.y + 62 * Math.sin(a)).toFixed(1) };
    link("internet", `strong ${live(inet.status) ? "live" : ""} ${inet.status === "unknown" ? "dashed" : ""}`, `M${C.x},${C.y} L${pos.internet.x},${pos.internet.y}`, col(inet.status));
  }
  devices.forEach((d) => {
    const p = pos[d.key];
    if (!p || !pos[p.hub]) return;
    link(d.key, `${live(d.status) ? "live" : ""} ${d.kind === "new" ? "dashed" : ""} ${d.suggested ? "sug" : ""}`, curve(pos[p.hub], p), d.suggested ? NO_SUGGEST.color : col(d.status));
  });

  // Icons are drawn as SVG paths, not <ha-icon> in a <foreignObject>: WebKit (Safari, the iOS/iPadOS companion
  // app) ignores the SVG's viewBox scaling for positioned elements inside a foreignObject, and ha-svg-icon is
  // position:relative, so every icon was drawn away from its circle. The path data comes from hidden <ha-icon>
  // elements outside the SVG (_resolveIcons).
  const iconFO = (p, r, icon, color) => {
    const size = r * 1.05;
    const d = NO_ICON_PATHS.get(icon);
    return `<g class="ic" data-icon="${noEsc(icon)}" transform="translate(${(p.x - size / 2).toFixed(1)} ${(p.y - size / 2).toFixed(1)}) scale(${(size / 24).toFixed(4)})" fill="${color}" pointer-events="none"><path d="${d ? noEsc(d) : ""}"/></g>`;
  };

  let gfx = "";
  let labels = "";
  // Text boxes of hub labels placed so far; the router's label always sits right below it.
  const placedLabels = [{ x0: C.x - 45, y0: C.y + 32 + 5, x1: C.x + 45, y1: C.y + 32 + 30 }];
  Object.values(hubs).forEach((h) => {
    const p = pos[h.key];
    if (!p) return;
    const isRoot = h.key === root;
    const isNew = h.kind === "newhub";
    const isGroup = h.kind === "group";
    const r = isRoot ? 32 : isGroup ? 19 : 25;
    const c = col(isNew ? "new" : h.status);
    let sub = isNew
      ? `${devices.filter((d) => d.kind === "new").length} waiting`
      : isGroup ? `${h.count} devices`
      : (cfg.show_ip && h.ip) ? h.ip : (NO_HUB_TYPE[h.hub_type] || "");
    if (String(sub).toLowerCase() === String(h.label).toLowerCase()) sub = "";
    // Label below, above, right or left: the first side whose text box doesn't touch another
    // label, a hub or a nearby device. Otherwise the side that overlaps least.
    const shown = noTrunc(h.label, 26);
    const w = Math.max(String(shown).length * (isGroup ? 7.4 : 8.6), String(sub).length * 6.8) + 10;
    const hgt = sub ? 38 : 22;
    const box = (s) => {
      const x0 = s.anchor === "middle" ? s.x - w / 2 : s.anchor === "start" ? s.x : s.x - w;
      return { x0, y0: s.y1 - 16, x1: x0 + w, y1: s.y1 - 16 + hgt };
    };
    const overlap = (a, b) => Math.max(0, Math.min(a.x1, b.x1) - Math.max(a.x0, b.x0)) * Math.max(0, Math.min(a.y1, b.y1) - Math.max(a.y0, b.y0));
    const square = (q, rr) => ({ x0: q.x - rr, y0: q.y - rr, x1: q.x + rr, y1: q.y + rr });
    const discs = Object.values(hubs).filter((o) => o.key !== h.key && pos[o.key])
      .map((o) => square(pos[o.key], (o.key === root ? 32 : o.kind === "group" ? 19 : 25) + 6));
    const near = devices.map((d) => pos[d.key]).filter((q) => q && Math.abs(q.x - p.x) < 220 && Math.abs(q.y - p.y) < 120).map((q) => square(q, 19));
    const cost = (s) => {
      const b = box(s);
      return placedLabels.reduce((n, o) => n + 4 * overlap(b, o), 0)
        + discs.reduce((n, o) => n + 3 * overlap(b, o), 0)
        + near.reduce((n, o) => n + overlap(b, o), 0);
    };
    const sides = [
      { side: "below", x: p.x, y1: p.y + r + 21, anchor: "middle" },
      { side: "above", x: p.x, y1: p.y - r - 27, anchor: "middle" },
      { side: "right", x: p.x + r + 10, y1: p.y - 2, anchor: "start" },
      { side: "left", x: p.x - r - 10, y1: p.y - 2, anchor: "end" },
    ];
    if (!(isRoot || p.y >= C.y - 4)) sides.unshift(sides.splice(1, 1)[0]);
    const pick = isRoot ? sides[0] : (sides.find((s) => cost(s) === 0) || sides.reduce((a, s) => (cost(s) < cost(a) ? s : a)));
    if (!isRoot) placedLabels.push(box(pick));
    const ly1 = pick.y1;
    const ly2 = ly1 + 16;
    const lx = pick.x;
    const anchor = pick.anchor;
    const target = edit && h.kind === "hub" ? "target" : "";
    gfx += `<g class="node hub ${isGroup ? "group" : ""} ${target}" data-key="${noEsc(h.key)}">` +
      (isGroup ? "" : `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${r + 9}" class="halo" fill="${c}"/>`) +
      `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${r}" class="disc ${isNew || isGroup ? "dashed" : ""} ${h.status === "stale" ? "pulse" : ""}" stroke="${c}"/>` +
      iconFO(p, r, h.icon, c) + `</g>`;
    scene.nodes.push({ k: h.key, x: p.x, y: p.y, r, color: c, icon: h.icon, hub: true, root: isRoot, halo: !isGroup, dashed: isNew || isGroup });
    // Hub labels go in their own layer, drawn after devices so nothing covers them.
    labels += `<g class="hlabels" data-k="${noEsc(h.key)}" pointer-events="none">` +
      `<text x="${lx.toFixed(1)}" y="${ly1.toFixed(1)}" class="hlab ${isGroup ? "glab" : ""}" style="text-anchor:${anchor}">${noEsc(shown)}</text>` +
      (sub ? `<text x="${lx.toFixed(1)}" y="${ly2.toFixed(1)}" class="hsub" style="text-anchor:${anchor}">${noEsc(sub)}</text>` : "") +
      `</g>`;
    scene.hubLabels.push({ k: h.key, x: lx, y: ly1, y2: ly2, text: shown, sub, anchor, group: isGroup });
  });

  devices.forEach((d) => {
    const p = pos[d.key];
    if (!p) return;
    const c = d.suggested ? NO_SUGGEST.color : col(d.status);
    const deg = (p.a * 180) / Math.PI;
    const nd = ((deg % 360) + 360) % 360;
    const flip = nd > 90 && nd < 270;
    const tr = p.r + 24;
    const tx = C.x + tr * Math.cos(p.a), ty = C.y + tr * Math.sin(p.a);
    const rot = flip ? deg + 180 : deg;
    const pinned = edit && d.placement === "manual";
    const text = noTrunc(d.label, maxChars);
    gfx += `<g class="node dev ${d.status} ${d.sleepy ? "sleepy" : ""} ${d.pending ? "pending" : ""} ${pinned ? "pinned" : ""} ${edit && d.kind === "device" ? "draggable" : ""}" data-key="${noEsc(d.key)}">` +
      (d.pending ? `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="21" class="pendring"/>` : "") +
      (live(d.status) ? `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="21" class="halo" fill="${c}"/>` : "") +
      (d.suggested ? `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="21" class="sugring"/>` : "") +
      `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="15" class="disc ${d.status === "stale" ? "pulse" : ""} ${d.kind === "new" ? "dashed" : ""}" stroke="${c}"/>` +
      iconFO(p, 15, d.icon, c) +
      (d.sleepy ? `<circle cx="${(p.x + 11).toFixed(1)}" cy="${(p.y - 11).toFixed(1)}" r="5" class="zz"/>` : "") +
      // Pinned by hand (edit mode): blue fill, the outline keeps its status color, plus a pin badge.
      (pinned ? `<g class="pin" pointer-events="none"><circle cx="${(p.x - 13).toFixed(1)}" cy="${(p.y - 13).toFixed(1)}" r="8.5"/>` +
        `<path d="${NO_PIN_PATH}" transform="translate(${(p.x - 19).toFixed(1)} ${(p.y - 19).toFixed(1)}) scale(0.5) rotate(30 12 12)"/></g>` : "") +
      `<text x="${tx.toFixed(1)}" y="${ty.toFixed(1)}" transform="rotate(${rot.toFixed(1)} ${tx.toFixed(1)} ${ty.toFixed(1)})" text-anchor="${flip ? "end" : "start"}" dominant-baseline="middle" class="dlab" style="font-size:${fs.toFixed(1)}px${d.suggested ? `;fill:${NO_SUGGEST.light}` : ""}">${noEsc(text)}</text></g>`;
    scene.nodes.push({ k: d.key, x: p.x, y: p.y, r: 15, color: c, icon: d.icon, halo: live(d.status), dashed: d.kind === "new", sleepy: d.sleepy });
    scene.devLabels.push({ k: d.key, x: tx, y: ty, rot, anchor: flip ? "end" : "start", size: fs, text,
      color: d.suggested ? NO_SUGGEST.light : ({ recent: "#fde68a", stale: "#fecaca", new: "#e9d5ff" }[d.status] || "#cbd5e1") });
  });

  if (inet) {
    const ip = pos.internet, c = col(inet.status);
    gfx += `<g class="node inet ${inet.status}" data-key="internet">` +
      (live(inet.status) ? `<circle cx="${ip.x}" cy="${ip.y}" r="21" class="halo" fill="${c}"/>` : "") +
      `<circle cx="${ip.x}" cy="${ip.y}" r="16" class="disc ${inet.status === "stale" ? "pulse" : ""}" stroke="${c}"/>` +
      iconFO(ip, 16, "mdi:web", c) + `</g>`;
    const text = inet.status === "stale" ? "Internet down" : "Internet";
    labels += `<g class="hlabels" data-k="internet" pointer-events="none"><text x="${ip.x + 24}" y="${ip.y + 5}" class="hlab ilab" style="text-anchor:start${inet.status === "stale" ? `;fill:${c}` : ""}">${text}</text></g>`;
    scene.nodes.push({ k: "internet", x: ip.x, y: ip.y, r: 16, color: c, icon: "mdi:web", halo: live(inet.status) });
    scene.hubLabels.push({ k: "internet", x: ip.x + 24, y: ip.y + 5, text, anchor: "start", inet: true, color: inet.status === "stale" ? c : null });
  }

  const counts = { online: 0, recent: 0, stale: 0, new: 0 };
  devices.forEach((d) => {
    if (d.kind === "more") { d.members.forEach((m) => { if (counts[m.status] !== undefined) counts[m.status]++; }); return; }
    if (counts[d.status] !== undefined && !String(d.key).startsWith("sug:")) counts[d.status]++;
  });
  return { L, pos, C, links, gfx, labels, counts, scene };
}

// ------------------------------------------------------------------ scanning

const NO_SCAN_PHASE = {
  starting: "Getting ready",
  ping: "Pinging every address",
  arp: "Reading the router's address table",
  names: "Asking devices their names",
  snmp: "Asking switches what is plugged in where",
  layout: "Working out the layout",
};

/** What the card shows for a network scan, from the summary sensor's attributes: "takeover" while the very first
 * scan on an install is running, "chip" for a later scan, "none" otherwise (and for older versions without these).
 * A first scan that is not running (it failed or has not started) never hides the map. */
function netorbitScanMode(attrs) {
  const a = attrs || {};
  if (a.first_scan_done === false && a.scanning === true) return "takeover";
  if (a.scanning === true) return "chip";
  return "none";
}

/** The words and numbers on the first-scan pill. */
function netorbitScanText(attrs) {
  const a = attrs || {};
  const found = Math.max(0, Math.round(Number(a.found) || 0));
  const pct = Math.max(0, Math.min(100, Math.round(Number(a.scan_progress) || 0)));
  return { found, pct, phase: NO_SCAN_PHASE[a.scan_phase] || NO_SCAN_PHASE.starting, line: `Scanning your network... found ${found} device${found === 1 ? "" : "s"}` };
}

const NO_BLUE = "#60a5fa";
// The radar used while scanning and in the reveal: dotted range rings, pulse rings and a sweeping beam.
// jsBeam: the beam and pulses are moved by script (the reveal), not by CSS animations.
function noRadarSvg(C, jsBeam) {
  const R = 420;
  const rad = (d) => ((d - 90) * Math.PI) / 180;
  let wedges = "";
  const N = 18, SPAN = 70;
  for (let i = 0; i < N; i++) {
    const a0 = (-i - 1) * (SPAN / N), a1 = -i * (SPAN / N);
    wedges += `<path d="M${C.x},${C.y} L${(C.x + R * Math.cos(rad(a0))).toFixed(1)},${(C.y + R * Math.sin(rad(a0))).toFixed(1)} A${R},${R} 0 0 1 ${(C.x + R * Math.cos(rad(a1))).toFixed(1)},${(C.y + R * Math.sin(rad(a1))).toFixed(1)} Z" fill="url(#nofade)" opacity="${(0.42 * (1 - i / N)).toFixed(3)}"/>`;
  }
  return `<g class="radar ${jsBeam ? "js" : ""}" pointer-events="none">` +
    `<defs><radialGradient id="nofade" cx="${C.x}" cy="${C.y}" r="${R}" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="${NO_BLUE}" stop-opacity=".55"/><stop offset="1" stop-color="${NO_BLUE}" stop-opacity="0"/></radialGradient></defs>` +
    [130, 250, 370].map((r) => `<circle class="st" cx="${C.x}" cy="${C.y}" r="${r}"/>`).join("") +
    [1, 2, 3].map((i) => `<circle class="pr p${i}" cx="${C.x}" cy="${C.y}" r="380"/>`).join("") +
    // The see-through full circle keeps the group's box centered on the router, so it turns around it.
    `<g class="beam"><circle cx="${C.x}" cy="${C.y}" r="${R}" fill="none" stroke="none"/>${wedges}` +
    `<line x1="${C.x}" y1="${C.y}" x2="${C.x}" y2="${C.y - R}" stroke="#bfdbfe" stroke-width="2" opacity=".7" stroke-linecap="round"/></g></g>`;
}

const NO_RADAR_ICON = `<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9.5" fill="none" stroke="${NO_BLUE}" stroke-width="1.6" opacity=".55"/>` +
  `<circle cx="12" cy="12" r="5" fill="none" stroke="${NO_BLUE}" stroke-width="1.4" opacity=".4"/><g class="rb"><circle cx="12" cy="12" r="9.5" fill="none"/><path d="M12 12 L12 2.5 A9.5 9.5 0 0 1 20.2 7.2 Z" fill="${NO_BLUE}" opacity=".55"/>` +
  `<line x1="12" y1="12" x2="12" y2="2.5" stroke="#bfdbfe" stroke-width="1.6"/></g><circle cx="12" cy="12" r="1.8" fill="#bfdbfe"/></svg>`;

// The reveal after the first scan, in seconds: the beam turns once and each node pops in as it passes, links
// draw in, then labels fade in and the normal header comes back.
const NO_REVEAL = { start: 0.12, turn: 2.05, labels: 2.6, header: 3.3, end: 4 };

function noClamp(x) { return Math.max(0, Math.min(1, x)); }
function noEase(x) { return 1 - Math.pow(1 - noClamp(x), 3); }

/** Angle of a point around the router, 0 at 12 o'clock, growing clockwise, in [0, 2π). */
function noBeamAngle(C, p) {
  const a = Math.atan2(p.y - C.y, p.x - C.x) + Math.PI / 2;
  return ((a % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);
}

/** Where the beam points at time t of the reveal, in degrees. Keeps turning at the same speed before 0. */
function noBeamDeg(t) {
  return ((((t - NO_REVEAL.start) / NO_REVEAL.turn) * 360) % 360 + 360) % 360;
}

/** State at time t of the reveal of a node at beam angle a: scale s, opacity o and how much of its link is drawn. */
function netorbitRevealNode(t, a, reduced) {
  const at = NO_REVEAL.start + (a / (2 * Math.PI)) * NO_REVEAL.turn;
  const since = (t - at) / 0.35;
  if (since <= 0) return { s: reduced ? 1 : 0, o: 0, draw: 0, at };
  if (reduced) return { s: 1, o: noClamp(since / 1.5), draw: 1, at };
  const k = noEase(since);
  const s = since >= 1 ? 1 : 0.35 + 0.65 * k + 0.18 * Math.sin(Math.PI * noClamp(since));
  return { s, o: noClamp(since * 1.6), draw: noEase((t - at) / 0.45), at };
}

/** Opacity of labels, radar and the result pill at time t of the reveal. */
function netorbitRevealGlobal(t) {
  return {
    labels: noClamp((t - NO_REVEAL.labels) / 0.5),
    radar: 1 - noClamp((t - NO_REVEAL.start - NO_REVEAL.turn) / 0.5),
    pill: 1 - noClamp((t - NO_REVEAL.header - 0.2) / 0.4),
    header: t >= NO_REVEAL.header,
  };
}

/** The first part (fraction f) of a link drawn as "M x,y L x,y" or "M x,y Q cx,cy x,y". */
function noPartialPath(d, f) {
  if (f >= 1) return d;
  const n = (String(d).match(/-?\d+(\.\d+)?/g) || []).map(Number);
  const lerp = (a, b) => a + (b - a) * f;
  const fx = (v) => v.toFixed(1);
  if (n.length === 6) {
    const ax = lerp(n[0], n[2]), ay = lerp(n[1], n[3]);
    const bx = lerp(n[2], n[4]), by = lerp(n[3], n[5]);
    return `M${fx(n[0])},${fx(n[1])} Q${fx(ax)},${fx(ay)} ${fx(ax + (bx - ax) * f)},${fx(ay + (by - ay) * f)}`;
  }
  if (n.length === 4) return `M${fx(n[0])},${fx(n[1])} L${fx(lerp(n[0], n[2]))},${fx(lerp(n[1], n[3]))}`;
  return d;
}

function noReducedMotion() {
  try { return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); } catch (e) { return false; }
}

// ------------------------------------------------------------------ sharing

// Generic names for shared maps, from the device's icon. First match wins.
const NO_SHARE_TYPES = [
  [/television-play|cast/, "Streamer"], [/cctv|doorbell|camera|webcam/, "Camera"], [/television|tv/, "TV"], [/soundbar/, "Soundbar"],
  [/speaker/, "Speaker"], [/gamepad|playstation|xbox|nintendo|controller/, "Console"], [/virtual-reality/, "VR headset"],
  [/thermostat/, "Thermostat"], [/laptop/, "Laptop"], [/monitor|desktop/, "Computer"], [/tablet|book-open/, "Tablet"],
  [/cellphone|phone/, "Phone"], [/watch/, "Watch"], [/printer/, "Printer"], [/server|nas/, "Storage"], [/home-assistant/, "Home Assistant"],
  [/power-socket|plug/, "Plug"], [/lightbulb|lamp|light/, "Light"], [/air-purifier|fan/, "Air purifier"], [/garage/, "Door opener"],
  [/fridge/, "Sensor"], [/sprinkler/, "Sprinkler"], [/robot/, "Vacuum"], [/^mdi:home/, "Smart home hub"], [/weather/, "Weather station"],
  [/smoke|alarm|bell-ring/, "Alarm"], [/chip|esp/, "Smart device"], [/access-point|router|switch/, "Network device"],
];

function noShareType(icon) {
  const s = String(icon || "");
  for (const [re, name] of NO_SHARE_TYPES) if (re.test(s)) return name;
  return "Device";
}

// Anything that looks like an IPv4 or IPv6 address or a MAC address, whole or in part. Word boundaries (\b) do not
// work here because "_" counts as a word character ("Cam_192.0.2.7"), so a match must sit between characters that
// are not letters or digits. The forms:
//   192.0.2.7                       IPv4
//   00:00:5e:00:53:01, 00-00-5e-00  MAC with separators, whole or partial (two or more pairs)
//   00005e005301, 0000.5e00.5301    MAC with no separators, and the dotted Cisco form
//   ESP_A1B2C3, cam-0a1b2c, Plug 00ff12   six hex characters after a separator, with at least one digit
//   2001:db8::1, fe80::, ::1, 2001:db8:0:0   IPv6 (a "::" next to hex, or four or more groups)
// Words made only of the letters a to f (Facade, Decade, Deadbeef) are left alone, since the six character rule
// needs a digit. A six digit number after a separator ("Room 100200") does count. A false hit only costs a
// generic label.
const NO_ADDRESS_RE = new RegExp(
  "(?:^|[^0-9a-z])(?:" +
  "\\d{1,3}(?:\\.\\d{1,3}){3}" +
  "|[0-9a-f]{2}(?:[:-][0-9a-f]{2})+" +
  "|[0-9a-f]{12}" +
  "|[0-9a-f]{4}(?:\\.[0-9a-f]{4}){2}" +
  "|[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*::(?:[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*)?" +
  "|::[0-9a-f]{1,4}(?::[0-9a-f]{1,4})*" +
  "|(?:[0-9a-f]{1,4}:){3,}[0-9a-f]{1,4}" +
  ")(?:[^0-9a-z]|$)" +
  "|[ _:.-](?=[0-9a-f]{0,5}\\d)[0-9a-f]{6}(?:[^0-9a-z]|$)",
  "i"
);

/** A copy of a map model that is safe to share: only what the drawing needs, with new keys, no addresses,
 * hostnames, makers or entity ids, and without devices waiting to be added or suggested hubs. With hideNames
 * (the default) devices are called by type and number ("TV 1", "Camera 2", numbered by hub, then by name) and
 * hubs by their type only. Exported for tests. */
function shareModel(model, opts) {
  const hide = !opts || opts.hideNames !== false;
  const src = model.hubs || {};
  const root = model.root;
  const byLabel = (a, b) => String(a.label).localeCompare(String(b.label)) || String(a.key).localeCompare(String(b.key));
  const real = Object.values(src).filter((h) => h.kind !== "newhub");
  const others = real.filter((h) => h.key !== root).sort(byLabel);
  // Positions on the map follow the card's order (by real name), kept as a bare rank so no name comes along.
  const rank = (list) => {
    const out = {};
    list.slice().sort(byLabel).forEach((n, i) => (out[n.key] = String(i).padStart(5, "0")));
    return out;
  };
  const hubRank = rank(others);
  const keyOf = {};
  if (src[root]) keyOf[root] = "h0";
  others.forEach((h, i) => (keyOf[h.key] = (h.kind === "group" ? "g" : "h") + (i + 1)));
  const safe = (s, fallback) => (NO_ADDRESS_RE.test(String(s || "")) || !String(s || "").trim() ? fallback : String(s));
  const hubType = (h) => (h.key === root ? "Router" : (h.hub_type && h.hub_type !== "other" && NO_HUB_TYPE[h.hub_type]) || "Hub");

  const hubs = {};
  (src[root] ? [src[root]] : []).concat(others).forEach((h) => {
    const key = keyOf[h.key];
    const parent = h.key === root ? null : (keyOf[h.parent] || "h0");
    const label = h.kind === "group" ? h.label : hide ? hubType(h) : safe(h.label, hubType(h));
    hubs[key] = { key, label, sort: hubRank[h.key], kind: h.kind, parent, link: parent, status: h.status, icon: h.icon, hub_type: NO_HUB_TYPE[h.hub_type] ? h.hub_type : undefined };
    if (h.kind === "group") hubs[key].count = 0;
  });

  const hubName = (k) => (src[k] ? String(src[k].label) : "");
  const kept = (model.devices || []).filter((d) => d.kind !== "new" && !d.suggested && src[d.link] && src[d.link].kind !== "newhub")
    .slice().sort((a, b) => hubName(a.link).localeCompare(hubName(b.link)) || byLabel(a, b));
  const devRank = rank(kept);
  const seen = {};
  const devices = kept.map((d, i) => {
    const key = "d" + (i + 1);
    const link = keyOf[d.link] || "h0";
    if (hubs[link] && hubs[link].kind === "group") hubs[link].count++;
    if (d.kind === "more") {
      const members = (d.members || []).map((m) => ({ status: m.status }));
      return { key, kind: "more", label: `+${members.length} more`, sort: devRank[d.key], link, parent: link, status: d.status, icon: d.icon, members };
    }
    const type = noShareType(d.icon);
    seen[type] = (seen[type] || 0) + 1;
    const generic = `${type} ${seen[type]}`;
    return { key, kind: "device", label: hide ? generic : safe(d.label, generic), sort: devRank[d.key], link, parent: link, status: d.status, icon: d.icon, sleepy: !!d.sleepy };
  });
  const internet = model.internet ? { key: "internet", kind: "internet", label: "Internet", status: model.internet.status } : null;
  return { hubs, devices, root: src[root] ? "h0" : null, summary: null, internet, suggestions: {}, title: hide ? "My home network" : null, sig: "" };
}

// Styles for the standalone share image (the card's own stylesheet doesn't come along).
const NO_SHARE_CSS = `.ring{fill:none;stroke:rgba(148,163,184,.07);stroke-width:1}.lk{fill:none;stroke-width:1.4;opacity:.45}.lk.strong{stroke-width:2.6;opacity:.75}` +
  `.lk.faint{stroke-width:1.2;opacity:.25;stroke-dasharray:2 5}.lk.dashed{stroke-dasharray:3 6}.lk.live{opacity:.8}.disc{fill:#0f172a;stroke-width:2.2}` +
  `.disc.dashed{stroke-dasharray:4 4}.halo{opacity:.13;filter:url(#noblur)}.hlab{fill:#f1f5f9;font-size:15px;font-weight:600;text-anchor:middle;paint-order:stroke;stroke:#0b1220;stroke-width:4px}` +
  `.hlab.glab{fill:#cbd5e1;font-size:13px;font-weight:500}.hlab.ilab{font-size:13px}.hsub{fill:#94a3b8;font-size:12px;text-anchor:middle;paint-order:stroke;stroke:#0b1220;stroke-width:4px}` +
  `.dlab{fill:#cbd5e1}.dev.recent .dlab{fill:#fde68a}.dev.stale .dlab{fill:#fecaca}.dev.sleepy:not(.online){opacity:.7}.zz{fill:#a78bfa;stroke:#0f172a;stroke-width:1.5}`;
const NO_SHARE_FONT = "Roboto, 'Helvetica Neue', Arial, system-ui, sans-serif";
// Size of the share image and video frame in map units: a header band, the map (1140 x 1090) and a footer line.
const NO_SHARE = { w: 1140, h: 1220, mapY: 80 };

function noShareCounts(smodel) {
  const counts = { online: 0, recent: 0, stale: 0 };
  smodel.devices.forEach((d) => {
    (d.kind === "more" ? d.members : [d]).forEach((m) => { if (counts[m.status] !== undefined) counts[m.status]++; });
  });
  return counts;
}

/** The share image as a standalone SVG document, built from a shareModel. */
function netorbitShareSvg(smodel, title) {
  const D = netorbitDraw(smodel, { show_ip: false }, {});
  const W = NO_SHARE.w, H = NO_SHARE.h;
  const counts = noShareCounts(smodel);
  let chips = "";
  let x = W - 24;
  ["stale", "recent", "online"].forEach((k) => {
    const text = `${counts[k]} ${NO_STATUS[k].label}`;
    const w = Math.round(text.length * 8.3 + 38);
    x -= w;
    chips += `<rect x="${x}" y="22" width="${w}" height="34" rx="17" fill="rgba(148,163,184,.08)" stroke="rgba(148,163,184,.18)"/>` +
      `<circle cx="${x + 18}" cy="39" r="4.5" fill="${NO_STATUS[k].color}"/><text x="${x + 30}" y="44.5" font-size="15" fill="#cbd5e1">${noEsc(text)}</text>`;
    x -= 10;
  });
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" font-family="${NO_SHARE_FONT}">` +
    `<defs><radialGradient id="nobg" cx="50%" cy="48%" r="70%"><stop offset="0" stop-color="#13213a"/><stop offset=".55" stop-color="#0b1220"/><stop offset="1" stop-color="#070b14"/></radialGradient>` +
    `<filter id="noblur" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="6"/></filter><style>${NO_SHARE_CSS}</style></defs>` +
    `<rect width="${W}" height="${H}" fill="url(#nobg)"/>` +
    `<path d="${NO_ORBIT_PATH}" fill="${NO_BLUE}" transform="translate(26 22) scale(1.4)"/>` +
    `<text x="70" y="49" font-size="26" font-weight="600" fill="#f1f5f9">${noEsc(noTrunc(title, 40))}</text>${chips}` +
    `<svg x="0" y="${NO_SHARE.mapY}" width="1140" height="1090" viewBox="-70 -40 1140 1090">` +
    `<circle class="ring" cx="${D.C.x}" cy="${D.C.y}" r="370"/>${D.links}${D.gfx}${D.labels}</svg>` +
    `<path d="${NO_ORBIT_PATH}" fill="${NO_BLUE}" transform="translate(${W - 196} ${H - 34}) scale(0.75)"/>` +
    `<text x="${W - 22}" y="${H - 20}" font-size="15" fill="#94a3b8" text-anchor="end">made with NetOrbit</text></svg>`;
}

/** Paints one frame of the share video on a 2D canvas context, scaled to the canvas width: the first scan (t < 0)
 * and then the reveal (t from 0 to NO_REVEAL.end and beyond). Everything is computed here, with no CSS
 * animation, so recording doesn't depend on the page. */
function netorbitPaintFrame(ctx, scene, t, o) {
  const W = NO_SHARE.w, H = NO_SHARE.h;
  const k = ctx.canvas.width / W;
  const C = scene.C;
  const g = netorbitRevealGlobal(t);
  ctx.setTransform(k, 0, 0, k, 0, 0);
  ctx.globalAlpha = 1;
  const bg = ctx.createRadialGradient(W / 2, H * 0.48, 0, W / 2, H * 0.48, W * 0.8);
  bg.addColorStop(0, "#13213a"); bg.addColorStop(0.55, "#0b1220"); bg.addColorStop(1, "#070b14");
  ctx.fillStyle = bg;
  ctx.fillRect(0, 0, W, H);
  const font = (size, weight) => `${weight || 400} ${size}px ${NO_SHARE_FONT}`;
  const icon = (d, x, y, size, color) => {
    if (!d) return;
    ctx.save();
    ctx.translate(x - size / 2, y - size / 2);
    ctx.scale(size / 24, size / 24);
    ctx.fillStyle = color;
    ctx.fill(new Path2D(d));
    ctx.restore();
  };
  const pill = (x, y, w, h, fill, stroke) => {
    const r = h / 2;
    ctx.beginPath();
    ctx.moveTo(x + r, y); ctx.lineTo(x + w - r, y); ctx.arc(x + w - r, y + r, r, -Math.PI / 2, Math.PI / 2);
    ctx.lineTo(x + r, y + h); ctx.arc(x + r, y + r, r, Math.PI / 2, (3 * Math.PI) / 2); ctx.closePath();
    ctx.fillStyle = fill; ctx.fill();
    if (stroke) { ctx.strokeStyle = stroke; ctx.lineWidth = 1; ctx.stroke(); }
  };
  const scanning = t < 0;
  const devCount = scene.nodes.filter((n) => !n.hub && n.k !== "internet").length;

  // Header.
  icon(NO_ORBIT_PATH, 43, 39, 34, NO_BLUE);
  ctx.textBaseline = "alphabetic";
  ctx.textAlign = "left";
  ctx.font = font(26, 600);
  ctx.fillStyle = "#f1f5f9";
  ctx.fillText(noTrunc(o.title, 40), 70, 49);
  ctx.font = font(15);
  if (!g.header) {
    const text = scanning ? `Scanning ${Math.round(o.pct(t))}%` : "Done";
    const w = ctx.measureText(text).width + 50;
    pill(W - 24 - w, 22, w, 34, "rgba(37,99,235,.14)", "rgba(96,165,250,.45)");
    ctx.strokeStyle = NO_BLUE; ctx.lineWidth = 1.5;
    ctx.beginPath(); ctx.arc(W - 24 - w + 21, 39, 8, 0, Math.PI * 2); ctx.stroke();
    ctx.fillStyle = "#bfdbfe";
    ctx.fillText(text, W - 24 - w + 36, 44.5);
  } else {
    let x = W - 24;
    ["stale", "recent", "online"].forEach((s) => {
      const text = `${o.counts[s]} ${NO_STATUS[s].label}`;
      const w = ctx.measureText(text).width + 40;
      x -= w;
      pill(x, 22, w, 34, "rgba(148,163,184,.08)", "rgba(148,163,184,.18)");
      ctx.fillStyle = NO_STATUS[s].color;
      ctx.beginPath(); ctx.arc(x + 18, 39, 4.5, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = "#cbd5e1";
      ctx.fillText(text, x + 30, 44.5);
      x -= 10;
    });
  }

  // Map, in its own coordinates.
  ctx.save();
  ctx.translate(70, NO_SHARE.mapY + 40);
  // Radar.
  if (g.radar > 0) {
    ctx.globalAlpha = g.radar;
    ctx.strokeStyle = NO_BLUE;
    ctx.lineWidth = 1;
    ctx.setLineDash([2, 6]);
    [130, 250, 370].forEach((r) => { ctx.globalAlpha = g.radar * 0.16; ctx.beginPath(); ctx.arc(C.x, C.y, r, 0, Math.PI * 2); ctx.stroke(); });
    ctx.setLineDash([]);
    for (let i = 0; i < 3; i++) {
      const ph = ((t + 10) / 3 + i / 3) % 1;
      ctx.globalAlpha = g.radar * 0.45 * (1 - ph);
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(C.x, C.y, 38 + ph * 342, 0, Math.PI * 2); ctx.stroke();
    }
    const deg = noBeamDeg(t);
    const R = 420;
    for (let i = 0; i < 18; i++) {
      const a1 = ((deg - (i * 70) / 18 - 90) * Math.PI) / 180, a0 = a1 - ((70 / 18) * Math.PI) / 180;
      const gr = ctx.createRadialGradient(C.x, C.y, 0, C.x, C.y, R);
      gr.addColorStop(0, "rgba(96,165,250,.55)"); gr.addColorStop(1, "rgba(96,165,250,0)");
      ctx.globalAlpha = g.radar * 0.42 * (1 - i / 18);
      ctx.fillStyle = gr;
      ctx.beginPath(); ctx.moveTo(C.x, C.y); ctx.arc(C.x, C.y, R, a0, a1); ctx.closePath(); ctx.fill();
    }
    const ab = ((deg - 90) * Math.PI) / 180;
    ctx.globalAlpha = g.radar * 0.7;
    ctx.strokeStyle = "#bfdbfe"; ctx.lineWidth = 2; ctx.lineCap = "round";
    ctx.beginPath(); ctx.moveTo(C.x, C.y); ctx.lineTo(C.x + R * Math.cos(ab), C.y + R * Math.sin(ab)); ctx.stroke();
    ctx.lineCap = "butt";
  }
  ctx.globalAlpha = 1;
  ctx.strokeStyle = "rgba(148,163,184,.07)"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.arc(C.x, C.y, 370, 0, Math.PI * 2); ctx.stroke();

  const state = {};
  scene.nodes.forEach((n) => {
    state[n.k] = n.root ? { s: 1, o: 1, draw: 1 } : scanning ? { s: 0, o: 0, draw: 0 } : netorbitRevealNode(t, noBeamAngle(C, n), false);
  });
  scene.links.forEach((l) => {
    const st = state[l.k];
    if (!st || st.draw <= 0) return;
    const strong = / strong/.test(" " + l.cls), faint = /faint/.test(l.cls);
    ctx.globalAlpha = faint ? 0.25 : strong ? 0.75 : / live/.test(" " + l.cls) ? 0.8 : 0.45;
    ctx.strokeStyle = l.color;
    ctx.lineWidth = strong ? 2.6 : faint ? 1.2 : 1.4;
    ctx.setLineDash(faint ? [2, 5] : /dashed/.test(l.cls) ? [3, 6] : []);
    ctx.stroke(new Path2D(noPartialPath(l.d, st.draw)));
  });
  ctx.setLineDash([]);
  scene.nodes.forEach((n) => {
    const st = state[n.k];
    if (!st || st.o <= 0) return;
    ctx.save();
    ctx.globalAlpha = st.o;
    ctx.translate(n.x, n.y); ctx.scale(st.s, st.s); ctx.translate(-n.x, -n.y);
    if (n.halo) {
      const hr = n.r + (n.hub ? 16 : 12);
      const gr = ctx.createRadialGradient(n.x, n.y, n.r * 0.6, n.x, n.y, hr);
      gr.addColorStop(0, n.color); gr.addColorStop(1, "rgba(0,0,0,0)");
      ctx.globalAlpha = st.o * 0.22;
      ctx.fillStyle = gr;
      ctx.beginPath(); ctx.arc(n.x, n.y, hr, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = st.o;
    }
    ctx.fillStyle = "#0f172a"; ctx.strokeStyle = n.color; ctx.lineWidth = 2.2;
    ctx.setLineDash(n.dashed ? [4, 4] : []);
    ctx.beginPath(); ctx.arc(n.x, n.y, n.r, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    ctx.setLineDash([]);
    icon(NO_ICON_PATHS.get(n.icon), n.x, n.y, n.r * 1.05, n.color);
    if (n.sleepy) {
      ctx.fillStyle = "#a78bfa"; ctx.strokeStyle = "#0f172a"; ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(n.x + 11, n.y - 11, 5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    }
    ctx.restore();
  });
  // Labels: the router's from the start, the rest once the beam has passed.
  ctx.textBaseline = "middle";
  scene.devLabels.forEach((l) => {
    if (g.labels <= 0) return;
    ctx.save();
    ctx.globalAlpha = g.labels;
    ctx.translate(l.x, l.y); ctx.rotate((l.rot * Math.PI) / 180);
    ctx.textAlign = l.anchor === "end" ? "right" : "left";
    ctx.font = font(l.size);
    ctx.fillStyle = l.color;
    ctx.fillText(l.text, 0, 0);
    ctx.restore();
  });
  ctx.textBaseline = "alphabetic";
  ctx.lineJoin = "round";
  scene.hubLabels.forEach((l) => {
    const isRoot = scene.nodes.some((n) => n.root && n.k === l.k);
    const a = isRoot ? 1 : g.labels;
    if (a <= 0) return;
    ctx.globalAlpha = a;
    ctx.textAlign = l.anchor === "middle" ? "center" : l.anchor === "end" ? "right" : "left";
    ctx.strokeStyle = "#0b1220"; ctx.lineWidth = 4;
    ctx.font = font(l.group || l.inet ? 13 : 15, l.group ? 500 : 600);
    ctx.fillStyle = l.color || (l.group ? "#cbd5e1" : "#f1f5f9");
    ctx.strokeText(l.text, l.x, l.y); ctx.fillText(l.text, l.x, l.y);
    if (l.sub) {
      ctx.font = font(12);
      ctx.fillStyle = "#94a3b8";
      ctx.strokeText(l.sub, l.x, l.y2); ctx.fillText(l.sub, l.x, l.y2);
    }
  });
  ctx.restore();
  ctx.globalAlpha = 1;

  // The pill at the bottom: scan progress, then the result.
  const pa = scanning ? 1 : g.pill;
  if (pa > 0) {
    ctx.globalAlpha = pa;
    const pw = 470, ph = scanning ? 92 : 64, px = (W - pw) / 2, py = NO_SHARE.mapY + 1090 - ph - 40;
    ctx.fillStyle = "rgba(11,18,32,.9)"; ctx.strokeStyle = "rgba(96,165,250,.35)"; ctx.lineWidth = 1;
    ctx.beginPath();
    if (ctx.roundRect) ctx.roundRect(px, py, pw, ph, 16); else ctx.rect(px, py, pw, ph);
    ctx.fill(); ctx.stroke();
    ctx.textAlign = "center";
    const found = scanning ? Math.round(o.found(t)) : devCount;
    ctx.font = font(19, 600);
    const l1 = scanning ? `Scanning your network... found ${found} device${found === 1 ? "" : "s"}` : `Found ${found} device${found === 1 ? "" : "s"}`;
    ctx.fillStyle = "#e2e8f0";
    ctx.fillText(l1, W / 2, py + 32);
    ctx.font = font(14);
    ctx.fillStyle = "#94a3b8";
    ctx.fillText(scanning ? o.phase(t) : "Your network map is ready", W / 2, py + 54);
    if (scanning) {
      ctx.fillStyle = "rgba(148,163,184,.18)"; ctx.fillRect(px + 24, py + 70, pw - 48, 4);
      ctx.fillStyle = NO_BLUE; ctx.fillRect(px + 24, py + 70, ((pw - 48) * o.pct(t)) / 100, 4);
    }
    ctx.globalAlpha = 1;
  }
  // Credit.
  ctx.textAlign = "right";
  ctx.font = font(15);
  ctx.fillStyle = "#94a3b8";
  ctx.fillText("made with NetOrbit", W - 22, H - 20);
  icon(NO_ORBIT_PATH, W - 187, H - 25, 18, NO_BLUE);
}

/** The best video type this browser can record a canvas to: mp4 where it can (Safari, newer Chrome), else webm. */
function noVideoType() {
  try {
    if (typeof MediaRecorder === "undefined" || typeof HTMLCanvasElement === "undefined" || !HTMLCanvasElement.prototype.captureStream) return null;
    const want = ["video/mp4;codecs=avc1", "video/mp4", "video/webm;codecs=vp9", "video/webm;codecs=vp8", "video/webm"];
    for (const t of want) if (!MediaRecorder.isTypeSupported || MediaRecorder.isTypeSupported(t)) return t;
  } catch (e) { /* not supported */ }
  return null;
}

// The whole map in SVG units. Zooming shows a part of it with the same aspect ratio, so the card keeps its size.
const NO_VIEW = { x: -70, y: -40, w: 1140, h: 1090 };
const NO_ZOOM_MAX = 5;

/** The view after a zoom or pan gesture. Pure; exported for tests.
 *  `view` is the view the gesture started from (null = the whole map), `anchor` the map point that was under the
 *  finger (or pointer, or the midpoint of a pinch) when it started, `now` the map point under it at this moment in
 *  that same starting view, and `factor` how much to zoom in (above 1) or out (below 1). The anchor stays under the
 *  finger, so a plain drag pans and a pinch zooms about the fingers. The result is kept inside the whole map and is
 *  null once zoomed all the way out. */
function netorbitZoomView(view, anchor, now, factor, base = NO_VIEW, max = NO_ZOOM_MAX) {
  const v = view || base;
  const scale = Math.min(max, Math.max(1, (base.w / v.w) * factor));
  const w = base.w / scale, h = base.h / scale;
  if (w >= base.w - 0.5) return null;
  const x = anchor.x - (now.x - v.x) * (w / v.w);
  const y = anchor.y - (now.y - v.y) * (h / v.h);
  return {
    x: Math.min(Math.max(x, base.x), base.x + base.w - w),
    y: Math.min(Math.max(y, base.y), base.y + base.h - h),
    w, h,
  };
}

class NetOrbitCard extends HTMLElement {
  static getStubConfig() {
    return { title: "Network" };
  }

  setConfig(config) {
    this._config = Object.assign(
      { title: "Network", height: 900, show_ip: false, show_legend: true, hide_sleepy_offline: false, group_by_type: true, show_internet: true, zoom: true },
      config || {}
    );
    this._sig = "";
    this._edit = false;
    this._view = null; // the part of the map on show; null is the whole map
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
  }

  getCardSize() { return 12; }
  getGridOptions() { return { columns: "full", rows: "auto", min_rows: 8 }; }

  set hass(hass) {
    this._hass = hass;
    // Never redraw under the user's finger. A zoom gesture whose end was never seen (a lost pointer event)
    // stops holding updates back after 15 s.
    if (this._drag || this._popOpen || this._revealing) return;
    // _held is set only here: a drag, an open popup or a reveal redraws by itself when it finishes (a drop moves the device or clears _sig, closing the popup redraws).
    if (this._zooming && Date.now() - this._zooming < 15000) { this._held = true; return; }
    this._held = false;
    const model = this._model();
    const mode = netorbitScanMode(model.summary && model.summary.attributes);
    if (mode === "takeover") {
      const t = this._takeoverModel(model);
      const r = t.hubs[t.root];
      const sig = JSON.stringify(["takeover", r.label, r.status, r.icon, t.internet && t.internet.status]);
      // The first scan always shows the whole map (no zoom), and so does the reveal after it.
      if (sig !== this._sig) { this._sig = sig; this._view = null; this._render(t, "takeover"); }
      this._updateScan(model);
      this._mode = "takeover";
      return;
    }
    const sig = JSON.stringify([model.sig, this._edit, mode]);
    if (sig === this._sig) return;
    this._sig = sig;
    // The first scan just finished: play the map in once.
    const reveal = this._mode === "takeover" && !!model.root;
    this._mode = mode;
    if (reveal) this._view = null;
    this._render(model, reveal ? "reveal" : mode);
    if (reveal) this._playReveal();
  }

  _isAdmin() {
    return !!(this._hass && this._hass.user && this._hass.user.is_admin);
  }

  _entities() {
    const hass = this._hass;
    const cfg = this._config;
    let ids = Object.keys(hass.states).filter((e) => e.startsWith("sensor.") && hass.states[e].attributes.netorbit === true);
    if (cfg.entities) ids = ids.filter((e) => cfg.entities.includes(e));
    if (cfg.device_id && hass.entities) ids = ids.filter((e) => (hass.entities[e] || {}).device_id === cfg.device_id);
    return ids.map((e) => hass.states[e]);
  }

  _model() {
    const hass = this._hass;
    const cfg = this._config;
    const states = this._entities();
    const hubs = {};
    const devices = [];
    let summary = null;
    let internet = null;
    let root = null;
    const sig = [];
    states.forEach((s) => {
      const a = s.attributes;
      if (a.kind === "summary") {
        summary = s;
        sig.push(JSON.stringify([a.new_devices, a.suggested_hubs, a.group_by_type]));
        return;
      }
      if (a.kind === "internet") {
        if (cfg.show_internet !== false) {
          internet = { key: "internet", entity: s.entity_id, kind: "internet", label: a.label || "Internet", status: NO_STATUS[s.state] ? s.state : "unknown", last_seen: a.last_seen };
          sig.push(["internet", internet.status].join("~"));
        }
        return;
      }
      const reg = (hass.entities || {})[s.entity_id] || {};
      const icon = reg.icon || a.icon || "mdi:lan-connect";
      const n = {
        // A name set in Home Assistant's entity settings wins over NetOrbit's own name.
        key: a.node_key, entity: s.entity_id, label: reg.name || a.label || a.friendly_name, ip: a.ip, mac: a.mac, ip_changes: a.ip_changes || 0,
        kind: a.kind, link: a.link, parent: a.link, sleepy: a.sleepy === true, last_seen: a.last_seen,
        hub_type: a.hub_type, hostname: a.hostname, vendor: a.vendor, placement: a.placement, port: a.port, network: a.network, vlan: a.vlan, icon,
        status: NO_STATUS[s.state] ? s.state : "unknown",
      };
      // A device just dropped on a hub is drawn there right away, with a "saving" ring, until
      // Home Assistant reports the new placement (or 10 s pass and the old one stands).
      const opt = this._optimistic && this._optimistic.get(n.key);
      if (opt) {
        if (n.link === opt.link || Date.now() - opt.t > 10000) this._optimistic.delete(n.key);
        else { n.link = n.parent = opt.link; n.placement = "manual"; n.pending = true; }
      }
      sig.push([n.key, n.status, n.link, n.label, icon, n.sleepy ? 1 : 0, n.placement, n.pending ? 1 : 0].join("~"));
      if (a.kind === "hub") {
        hubs[n.key] = n;
        if (!n.link && !root) root = n.key;
      } else if (!(cfg.hide_sleepy_offline && n.sleepy && n.status !== "online")) {
        devices.push(n);
      }
    });
    if (!root && Object.keys(hubs).length) root = Object.keys(hubs)[0];
    if (!root) return { hubs, devices, root, summary, internet, sig: sig.join("|"), suggestions: {} };

    // Suggested hubs: mark the device if we track it, otherwise show it in the "new" bubble.
    const suggestions = {};
    ((summary && summary.attributes.suggested_hubs) || []).forEach((s) => (suggestions[String(s.mac || "").toUpperCase()] = s));
    devices.forEach((d) => { if (d.mac && suggestions[d.mac]) d.suggested = suggestions[d.mac]; });

    const fresh = summary ? summary.attributes.new_devices || [] : [];
    const tracked = new Set(devices.map((d) => d.mac));
    const loose = Object.values(suggestions).filter((s) => !tracked.has(String(s.mac).toUpperCase()));
    if ((fresh.length || loose.length) && root) {
      hubs.__new = { key: "__new", label: "New devices", kind: "newhub", parent: root, status: "new", icon: "mdi:tag-plus-outline" };
      fresh.forEach((d) => devices.push({
        key: "new:" + d.key, raw: d, label: d.name || d.mac || d.ip, ip: d.ip, mac: d.mac, kind: "new", link: "__new",
        status: "new", icon: "mdi:help-circle-outline", last_seen: d.last_seen, vendor: d.vendor,
      }));
      loose.forEach((s) => devices.push({
        key: "sug:" + s.key, raw: s, label: s.name, ip: s.ip, mac: s.mac, kind: "new", link: "__new",
        status: "new", icon: "mdi:access-point-network", suggested: s,
      }));
    }

    // Group devices by type when nothing knows where they connect.
    const wantGroups = cfg.group_by_type !== false && (!summary || summary.attributes.group_by_type !== false);
    const loners = devices.filter((d) => d.kind === "device" && d.link === root && d.placement !== "manual");
    if (wantGroups && loners.length >= 8) {
      const byGroup = {};
      loners.forEach((d) => (byGroup[noGroupFor(d.icon)] = (byGroup[noGroupFor(d.icon)] || []).concat(d)));
      // Single devices stay on the router; a group of one is just noise.
      Object.keys(byGroup).forEach((gid) => { if (byGroup[gid].length < 2) delete byGroup[gid]; });
      if (Object.keys(byGroup).length > 1) {
        Object.entries(byGroup).forEach(([gid, list]) => {
          const g = NO_GROUPS.find((x) => x.id === gid) || { label: "Other", icon: "mdi:devices" };
          const key = "__grp_" + gid;
          hubs[key] = { key, label: g.label, kind: "group", parent: root, status: "group", icon: g.icon, count: list.length };
          list.forEach((d) => (d.link = key));
        });
      }
    }
    // Very large networks: keep problem devices visible, fold the rest into "+N more" per hub.
    const max = Number(cfg.max_devices) || 80;
    if (devices.length > max) {
      const rank = { stale: 0, recent: 1, new: 2, unknown: 3, online: 4 };
      const byHub = {};
      devices.forEach((d) => (byHub[d.link] = (byHub[d.link] || []).concat(d)));
      const share = Math.max(3, Math.floor(max / Object.keys(byHub).length));
      const kept = [];
      Object.entries(byHub).forEach(([hub, list]) => {
        // Possible hubs first (they need a tap), then problems, then the rest.
        list.sort((a, b) => (!!b.suggested - !!a.suggested) || (rank[a.status] - rank[b.status]) || String(a.label).localeCompare(String(b.label)));
        if (list.length <= share) { kept.push(...list); return; }
        const shown = list.slice(0, share - 1);
        const rest = list.slice(share - 1);
        kept.push(...shown, {
          key: "more:" + hub, kind: "more", link: hub, label: `+${rest.length} more`, status: "online",
          icon: "mdi:dots-horizontal-circle-outline", members: rest,
        });
      });
      devices.length = 0;
      devices.push(...kept);
    }
    return { hubs, devices, root, summary, internet, suggestions, sig: sig.join("|") };
  }

  // mode: "none", "chip" (a later scan runs), "takeover" (the first scan runs) or "reveal" (the first map arrives
  // and plays in; see _playReveal).
  _render(model, mode) {
    const cfg = this._config;
    const { hubs, devices, root, summary } = model;
    mode = mode || "none";
    if (!root) {
      this.shadowRoot.innerHTML = `<ha-card header="${noEsc(cfg.title)}"><div style="padding:16px">No NetOrbit sensors found. Add the NetOrbit integration under Settings, Devices &amp; services.</div></ha-card>`;
      return;
    }
    this._model_ = model;
    const edit = this._edit && mode !== "takeover" && mode !== "reveal";
    const D = netorbitDraw(model, cfg, { edit });
    const pos = D.pos;
    this._pos = pos;
    const C = D.C;
    const counts = D.counts;
    const admin = this._isAdmin();
    const nSug = Object.keys(model.suggestions || {}).length;
    const chip = (k) => `<span class="chip"><i style="background:${NO_STATUS[k].color}"></i>${counts[k]} ${NO_STATUS[k].label}</span>`;
    const staleH = summary ? summary.attributes.stale_hours : 24;
    const grouped = Object.values(hubs).some((h) => h.kind === "group");
    const takeover = mode === "takeover";
    const scan = netorbitScanText(summary && summary.attributes);
    const share = cfg.show_share !== false && !takeover;
    const realDevices = devices.filter((d) => d.kind === "device" || d.kind === "more")
      .reduce((n, d) => n + (d.kind === "more" ? d.members.length : 1), 0);
    const scanChip = takeover
      ? `<span class="scanchip">${NO_RADAR_ICON}<span class="pct">Scanning ${scan.pct}%</span></span>`
      : mode === "reveal" ? `<span class="scanchip">${NO_RADAR_ICON}<span class="pct">Done</span></span>`
      : mode === "chip" ? `<span class="scanchip small" title="NetOrbit is checking the network for new devices">${NO_RADAR_ICON}Scanning...</span>` : "";
    const pill = takeover
      ? `<div class="scanpill"><div class="l1">Scanning your network... found <b class="found">${scan.found}</b> <span class="noun">device${scan.found === 1 ? "" : "s"}</span></div>` +
        `<div class="l2">${noEsc(scan.phase)}</div><div class="bar"><i style="width:${scan.pct}%"></i></div></div>`
      : mode === "reveal" ? `<div class="scanpill"><div class="l1">Found <b>${realDevices}</b> device${realDevices === 1 ? "" : "s"}</div><div class="l2">Your network map is ready</div></div>` : "";

    this.shadowRoot.innerHTML = `
<style>
  :host { display:block; }
  ha-card { position:relative; font-family: var(--ha-font-family-body, Roboto, system-ui, sans-serif); overflow:hidden; background: radial-gradient(ellipse at 50% 48%, #13213a 0%, #0b1220 55%, #070b14 100%); color:#e2e8f0; border:1px solid rgba(148,163,184,.12); }
  .head { display:flex; flex-wrap:wrap; align-items:center; gap:10px 12px; padding:16px 18px 4px; }
  .title { font-size:20px; font-weight:600; margin-right:auto; display:flex; align-items:center; gap:8px; }
  .title ha-icon { --mdc-icon-size:22px; color:#60a5fa; }
  .chip { display:inline-flex; align-items:center; gap:6px; font-size:12.5px; color:#cbd5e1; background:rgba(148,163,184,.08); border:1px solid rgba(148,163,184,.14); padding:4px 10px; border-radius:999px; font-variant-numeric:tabular-nums; }
  .chip i { width:8px; height:8px; border-radius:50%; display:inline-block; }
  .chip.sug { border-color:rgba(244,114,182,.5); color:#f9a8d4; }
  .editbtn { font:inherit; font-size:12.5px; display:inline-flex; align-items:center; gap:6px; padding:4px 12px; border-radius:999px; border:1px solid rgba(96,165,250,.45); background:transparent; color:#bfdbfe; cursor:pointer; }
  .editbtn.on { background:#2563eb; border-color:#2563eb; color:#fff; }
  .editbtn ha-icon { --mdc-icon-size:16px; }
  .banner { margin:6px 18px 0; padding:8px 12px; border-radius:8px; background:rgba(37,99,235,.15); border:1px solid rgba(96,165,250,.35); font-size:13px; color:#dbeafe; }
  .wrap { position:relative; }
  svg { font-family: var(--ha-font-family-body, Roboto, system-ui, sans-serif); width:100%; height:auto; display:block; max-height:${noHeight(cfg.height)}; user-select:none; -webkit-user-select:none; }
  .ring { fill:none; stroke:rgba(148,163,184,.07); stroke-width:1; }
  .lk { fill:none; stroke-width:1.4; opacity:.45; }
  .lk.strong { stroke-width:2.6; opacity:.75; }
  .lk.faint { stroke-width:1.2; opacity:.25; stroke-dasharray:2 5; }
  .lk.dashed { stroke-dasharray:3 6; }
  .lk.live { stroke-dasharray:6 10; animation: flow 1.6s linear infinite; opacity:.8; }
  .lk.strong.live { stroke-dasharray:10 8; }
  @keyframes flow { to { stroke-dashoffset:-32; } }
  .disc { fill:#0f172a; stroke-width:2.2; }
  .disc.dashed { stroke-dasharray:4 4; }
  .halo { opacity:.13; filter:url(#noblur); }
  .sugring { fill:none; stroke:#f472b6; stroke-width:2; stroke-dasharray:3 3; animation: spin 6s linear infinite; transform-box:fill-box; transform-origin:center; }
  @keyframes spin { to { transform:rotate(360deg); } }
  .node.pinned .disc { fill:rgba(29,78,216,.55); }
  .pin circle { fill:#2563eb; stroke:#0b1220; stroke-width:2; }
  .pin path { fill:#fff; }
  .pulse { animation: pulse 1.8s ease-in-out infinite; }
  @keyframes pulse { 50% { stroke-width:5; } }
  .hlab { fill:#f1f5f9; font-size:15px; font-weight:600; text-anchor:middle; paint-order:stroke; stroke:#0b1220; stroke-width:4px; }
  .hlab.glab { fill:#cbd5e1; font-size:13px; font-weight:500; }
  .hlab.ilab { font-size:13px; font-weight:600; }
  .hsub { fill:#94a3b8; font-size:12px; text-anchor:middle; paint-order:stroke; stroke:#0b1220; stroke-width:4px; }
  .dlab { fill:#cbd5e1; }
  .dev.recent .dlab { fill:#fde68a; }
  .dev.stale .dlab { fill:#fecaca; }
  .dev.new .dlab { fill:#e9d5ff; }
  .dev.sleepy:not(.online) { opacity:.7; }
  .zz { fill:#a78bfa; stroke:#0f172a; stroke-width:1.5; }
  .node { cursor:pointer; }
  .node.draggable { cursor:grab; }
  .node:hover .disc { stroke-width:4; }
  .node:hover .dlab { fill:#fff; font-weight:600; }
  .node.hot .disc { stroke:#60a5fa !important; stroke-width:6; }
  .pendring { fill:none; stroke:#60a5fa; stroke-width:2.5; stroke-dasharray:10 8; animation: spin 1.2s linear infinite; transform-box:fill-box; transform-origin:center; }
  .ghost { fill:rgba(96,165,250,.25); stroke:#60a5fa; stroke-width:2; pointer-events:none; }
  .zoomctl { position:absolute; right:10px; bottom:10px; display:flex; flex-direction:column; gap:6px; z-index:1; }
  .zoomctl button { width:32px; height:32px; padding:0; border-radius:50%; border:1px solid rgba(148,163,184,.3); background:rgba(15,23,42,.85); color:#cbd5e1; cursor:pointer; display:flex; align-items:center; justify-content:center; }
  .zoomctl button:hover { border-color:rgba(96,165,250,.6); color:#fff; }
  .zoomctl ha-icon { --mdc-icon-size:18px; }
  .zoomctl .zfit { display:none; }
  .wrap.zoomed .zoomctl .zfit { display:flex; }
  .tip { position:absolute; pointer-events:none; background:rgba(15,23,42,.96); border:1px solid rgba(148,163,184,.25); border-radius:10px; padding:10px 12px; font-size:12.5px; line-height:1.55; color:#e2e8f0; min-width:190px; max-width:280px; box-shadow:0 10px 30px rgba(0,0,0,.45); opacity:0; transition:opacity .12s; z-index:2; }
  .tiptip { margin-top:6px; padding-top:6px; border-top:1px solid rgba(148,163,184,.2); color:#93c5fd; }
  .tip b { font-size:13.5px; display:block; margin-bottom:2px; }
  .tip .k { color:#94a3b8; display:inline-block; width:74px; }
  .pop { position:absolute; background:rgba(15,23,42,.98); border:1px solid rgba(96,165,250,.45); border-radius:12px; padding:12px; font-size:13px; color:#e2e8f0; z-index:3; width:250px; box-shadow:0 10px 30px rgba(0,0,0,.5); }
  .pop b { display:block; font-size:14px; }
  .pop .muted { color:#94a3b8; font-size:12px; margin-top:2px; }
  .pop .row { display:flex; gap:8px; margin-top:10px; }
  .pop label { display:block; color:#94a3b8; font-size:12px; margin-top:10px; }
  .pop select, .pop input { width:100%; box-sizing:border-box; margin-top:4px; font:inherit; padding:6px 8px; border-radius:8px; border:1px solid rgba(148,163,184,.3); background:#0b1220; color:#e2e8f0; }
  .pop button { flex:1; font:inherit; padding:7px 10px; border-radius:8px; border:1px solid rgba(148,163,184,.3); background:#1e293b; color:#e2e8f0; cursor:pointer; }
  .pop button.go { background:#2563eb; border-color:#2563eb; }
  .pop button.warn { border-color:rgba(248,113,113,.5); color:#fecaca; }
  .mlist { max-height:240px; overflow:auto; margin-top:8px; }
  .mrow { display:flex; align-items:center; gap:8px; padding:4px 2px; cursor:pointer; border-radius:6px; }
  .mrow:hover { background:rgba(148,163,184,.1); }
  .mrow i { width:8px; height:8px; border-radius:50%; flex:none; }
  .mrow span { margin-left:auto; color:#94a3b8; font-size:12px; }
  .legend { display:flex; flex-wrap:wrap; gap:14px; padding:2px 18px 14px; font-size:12px; color:#94a3b8; }
  .legend span { display:inline-flex; align-items:center; gap:6px; }
  .legend .dot { width:9px; height:9px; border-radius:50%; display:inline-block; }
  .sharebtn { font:inherit; font-size:12.5px; display:inline-flex; align-items:center; gap:6px; padding:4px 12px; border-radius:999px; border:1px solid rgba(148,163,184,.35); background:transparent; color:#e2e8f0; cursor:pointer; }
  .sharebtn ha-icon { --mdc-icon-size:16px; }
  .scanchip { display:inline-flex; align-items:center; gap:7px; font-size:12.5px; color:#bfdbfe; background:rgba(37,99,235,.14); border:1px solid rgba(96,165,250,.45); padding:4px 11px 4px 8px; border-radius:999px; font-variant-numeric:tabular-nums; }
  .scanchip svg { width:15px; height:15px; display:block; }
  .scanchip .rb { animation: spin 1.6s linear infinite; transform-box:fill-box; transform-origin:center; }
  .revealing .head > .chip, .revealing .head > .sharebtn, .revealing .head > .editbtn { display:none; }
  .revealing .legend, .legend.hide { visibility:hidden; }
  .scanpill { position:absolute; left:50%; bottom:3.5%; transform:translateX(-50%); text-align:center; pointer-events:none; background:rgba(11,18,32,.88); border:1px solid rgba(96,165,250,.35); border-radius:14px; padding:10px 18px 9px; box-shadow:0 8px 30px rgba(0,0,0,.45); min-width:min(300px, 84%); max-width:92%; box-sizing:border-box; }
  .scanpill .l1 { font-size:15px; font-weight:600; color:#e2e8f0; font-variant-numeric:tabular-nums; }
  .scanpill .l1 b { color:#93c5fd; }
  .scanpill .l2 { font-size:12.5px; color:#94a3b8; margin-top:3px; }
  .scanpill .bar { height:3px; border-radius:2px; background:rgba(148,163,184,.18); margin-top:8px; overflow:hidden; }
  .scanpill .bar i { display:block; height:100%; background:#60a5fa; border-radius:2px; transition:width .6s ease; }
  .radar .st { fill:none; stroke:#60a5fa; stroke-width:1; opacity:.12; stroke-dasharray:2 6; }
  .radar .pr { fill:none; stroke:#60a5fa; stroke-width:1.5; vector-effect:non-scaling-stroke; opacity:0; transform-box:fill-box; transform-origin:center; animation: ringpulse 3s ease-out infinite; }
  .radar .pr.p2 { animation-delay:-1s; }
  .radar .pr.p3 { animation-delay:-2s; }
  @keyframes ringpulse { 0% { transform:scale(.1); opacity:.4; } 100% { transform:scale(1); opacity:0; } }
  .radar .beam { animation: spin 3s linear infinite; transform-box:fill-box; transform-origin:center; }
  .radar.js .beam, .radar.js .pr { animation:none; }
  .sharepop { position:absolute; top:58px; right:14px; width:320px; max-width:calc(100% - 28px); max-height:calc(100% - 70px); overflow:auto; box-sizing:border-box; background:rgba(15,23,42,.98); border:1px solid rgba(96,165,250,.45); border-radius:14px; padding:14px; color:#e2e8f0; font-size:13px; box-shadow:0 14px 40px rgba(0,0,0,.6); z-index:5; }
  .sharepop .shead { display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; }
  .sharepop .shead b { font-size:15px; }
  .sharepop .x { flex:none; width:28px; height:28px; padding:0; border:none; background:transparent; color:#94a3b8; font:inherit; font-size:20px; line-height:1; cursor:pointer; }
  .sharepop .seg { display:flex; gap:4px; padding:3px; border-radius:10px; background:#0b1220; border:1px solid rgba(148,163,184,.2); margin-bottom:10px; }
  .sharepop .seg button { flex:1; font:inherit; font-size:12.5px; padding:5px 8px; border:none; border-radius:7px; background:transparent; color:#94a3b8; cursor:pointer; }
  .sharepop .seg button.on { background:#1e293b; color:#e2e8f0; }
  .sharepop .thumb { width:100%; height:auto; max-height:34vh; object-fit:contain; border-radius:8px; border:1px solid rgba(148,163,184,.2); display:block; background:#0b1220; }
  .sharepop .trow { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-top:12px; }
  .sharepop .muted { color:#94a3b8; font-size:12px; margin-top:4px; line-height:1.4; }
  .sharepop .tg { flex:none; width:38px; height:22px; border-radius:11px; background:#475569; position:relative; border:none; padding:0; cursor:pointer; }
  .sharepop .tg i { position:absolute; left:3px; top:3px; width:16px; height:16px; border-radius:50%; background:#fff; transition:left .15s; }
  .sharepop .tg.on { background:#2563eb; }
  .sharepop .tg.on i { left:19px; }
  .sharepop .btns { display:flex; gap:8px; margin-top:14px; }
  .sharepop .btns button { flex:1; font:inherit; white-space:nowrap; padding:8px 10px; border-radius:9px; border:1px solid rgba(148,163,184,.3); background:#1e293b; color:#e2e8f0; display:flex; align-items:center; justify-content:center; gap:6px; cursor:pointer; }
  .sharepop .btns button.go { background:#2563eb; border-color:#2563eb; }
  .sharepop .btns button[disabled] { opacity:.6; cursor:default; }
  .sharepop .btns ha-icon { --mdc-icon-size:16px; }
  .sharepop .status { color:#94a3b8; font-size:12px; margin-top:8px; }
  .sharepop .status:empty { display:none; }
  .sharepop [hidden] { display:none !important; }
  .sharepop .replay { display:block; margin:12px auto 0; font:inherit; font-size:12px; color:#93c5fd; background:none; border:none; padding:2px; cursor:pointer; text-decoration:underline; }
  @media (max-width: 520px) {
    .scanpill { padding:7px 12px 7px; bottom:2%; }
    .scanpill .l1 { font-size:13px; }
    .scanpill .l2 { font-size:11.5px; }
    .scanpill .bar { margin-top:6px; }
  }
  @media (prefers-reduced-motion: reduce) {
    .lk.live, .pulse, .sugring, .scanchip .rb, .radar .beam, .radar .pr { animation:none; }
    .radar .beam, .radar .pr { display:none; }
    .radar .st { opacity:.22; }
  }
</style>
<ha-card class="${mode === "reveal" ? "revealing" : ""}">
  <div class="head">
    <div class="title"><ha-icon icon="mdi:orbit"></ha-icon>${noEsc(cfg.title)}</div>
    ${takeover ? scanChip : `${chip("online")}${chip("recent")}${chip("stale")}${counts.new ? chip("new") : ""}
    ${nSug ? `<span class="chip sug"><i style="background:${NO_SUGGEST.color}"></i>${nSug} possible hub${nSug > 1 ? "s" : ""}</span>` : ""}
    ${scanChip}
    ${share ? `<button class="sharebtn" title="Share an image or video of your map"><ha-icon icon="mdi:share-variant"></ha-icon>Share</button>` : ""}
    ${admin ? `<button class="editbtn ${edit ? "on" : ""}"><ha-icon icon="${edit ? "mdi:check" : "mdi:pencil"}"></ha-icon>${edit ? "Done" : "Edit layout"}</button>` : ""}`}
  </div>
  ${edit ? `<div class="banner">Drag a device onto the hub it connects through, or tap it for more options. Tap a hub to rename it, change its type, move it under another hub, or remove it.</div>` : ""}
  <div class="wrap">
    <svg class="map" viewBox="${NO_VIEW.x} ${NO_VIEW.y} ${NO_VIEW.w} ${NO_VIEW.h}" role="img" aria-label="Network map">
      <defs><filter id="noblur" x="-50%" y="-50%" width="200%" height="200%"><feGaussianBlur stdDeviation="6"/></filter></defs>
      ${takeover || mode === "reveal" ? noRadarSvg(C, mode === "reveal") : ""}
      <circle class="ring" cx="${C.x}" cy="${C.y}" r="370"/>
      ${D.links}
      ${D.gfx}${D.labels}
      <circle class="ghost" r="17" cx="-500" cy="-500"/>
    </svg>
    <div class="tip"></div>
    ${pill}
    ${cfg.zoom !== false ? `<div class="zoomctl"><button class="zin" title="Zoom in" aria-label="Zoom in"><ha-icon icon="mdi:plus"></ha-icon></button><button class="zout" title="Zoom out" aria-label="Zoom out"><ha-icon icon="mdi:minus"></ha-icon></button><button class="zfit" title="Whole map" aria-label="Whole map"><ha-icon icon="mdi:fit-to-screen-outline"></ha-icon></button></div>` : ""}
  </div>
  ${cfg.show_legend ? `<div class="legend ${takeover ? "hide" : ""}">
    <span>Moving line = answering pings</span>
    <span><i class="dot" style="background:${NO_STATUS.recent.color}"></i>Seen in the last ${noEsc(staleH)} h</span>
    <span><i class="dot" style="background:${NO_STATUS.stale.color}"></i>Not seen for ${noEsc(staleH)} h+</span>
    <span><i class="dot" style="background:#a78bfa"></i>Allowed to sleep</span>
    ${nSug ? `<span><i class="dot" style="background:${NO_SUGGEST.color}"></i>Might be a hub, tap to confirm</span>` : ""}
    ${grouped ? `<span>Grouped by type until NetOrbit knows where they connect</span>` : ""}
    ${cfg.zoom !== false ? `<span>Pinch, double-tap or Ctrl/Cmd+scroll to zoom</span>` : ""}
  </div>` : ""}
</ha-card>`;

    const btn = this.shadowRoot.querySelector(".editbtn");
    if (btn) btn.addEventListener("click", () => { this._edit = !this._edit; this._sig = ""; this.hass = this._hass; });
    const sbtn = this.shadowRoot.querySelector(".sharebtn");
    if (sbtn) sbtn.addEventListener("click", () => this._sharePopup());
    this._wire(model);
    this._wireZoom();
    this._resolveIcons();
  }

  // The first scan: only the router, with the radar, until the scan finishes and the map plays in.
  _takeoverModel(model) {
    const hubs = {};
    let root = model.root;
    if (root) hubs[root] = model.hubs[root];
    else {
      root = "__scan_root";
      hubs[root] = { key: root, label: "Router", kind: "hub", status: "online", icon: "mdi:router-network", hub_type: "router" };
    }
    return { hubs, devices: [], root, summary: model.summary, internet: model.internet, suggestions: {}, sig: "" };
  }

  // Scan progress changes often during the first scan. Update the words in place, so the radar keeps turning.
  _updateScan(model) {
    const s = netorbitScanText(model.summary && model.summary.attributes);
    const q = (sel) => this.shadowRoot.querySelector(sel);
    if (q(".scanchip .pct")) q(".scanchip .pct").textContent = `Scanning ${s.pct}%`;
    if (q(".scanpill .found")) q(".scanpill .found").textContent = String(s.found);
    if (q(".scanpill .noun")) q(".scanpill .noun").textContent = s.found === 1 ? "device" : "devices";
    if (q(".scanpill .l2")) q(".scanpill .l2").textContent = s.phase;
    if (q(".scanpill .bar i")) q(".scanpill .bar i").style.width = s.pct + "%";
  }

  /** Play the scan animation again on the map as it is now. */
  replayScan() {
    if (this._mode === "takeover" || !this._hass) return;
    const pop = this.shadowRoot.querySelector(".sharepop");
    if (pop) pop.remove();
    this._popOpen = false;
    const model = this._model();
    if (!model.root) return;
    this._view = null;
    this._render(model, "reveal");
    this._playReveal();
  }

  // The map has just arrived after the first scan, drawn with mode "reveal". The beam turns once and every node
  // pops in as it passes, its link draws in, then the labels fade in and the normal header comes back. The state
  // at each moment comes from netorbitRevealNode/netorbitRevealGlobal, the same timing the share video uses.
  _playReveal() {
    const sr = this.shadowRoot;
    const svg = sr.querySelector("svg.map");
    const model = this._model_;
    if (!svg || !model) return;
    const reduced = noReducedMotion();
    const C = this._pos[model.root];
    const nodes = [...svg.querySelectorAll(".node[data-key]")].map((el) => {
      const k = el.dataset.key;
      const p = this._pos[k];
      return { el, k, p, a: p ? noBeamAngle(C, p) : 0, root: k === model.root, lab: el.querySelector("text.dlab") };
    }).filter((n) => n.p);
    const byKey = {};
    nodes.forEach((n) => (byKey[n.k] = n));
    const links = [...svg.querySelectorAll("path.lk[data-k]")].map((p) => ({ p, d: p.getAttribute("d"), n: byKey[p.dataset.k] }));
    const hlabels = [...svg.querySelectorAll("g.hlabels[data-k]")].filter((g) => g.dataset.k !== model.root);
    const radar = svg.querySelector(".radar");
    const beam = radar && radar.querySelector(".beam");
    const pulses = radar ? [...radar.querySelectorAll(".pr")] : [];
    const card = sr.querySelector("ha-card");
    const chipEl = sr.querySelector(".head .scanchip");
    const pill = sr.querySelector(".scanpill");
    links.forEach((l) => (l.p.style.animation = "none"));
    const frame = (t) => {
      nodes.forEach((n) => {
        if (n.root) return;
        n.st = netorbitRevealNode(t, n.a, reduced);
        n.el.style.opacity = n.st.o >= 1 ? "" : n.st.o.toFixed(3);
        const s = n.st.s;
        n.el.setAttribute("transform", s === 1 ? "" : `translate(${n.p.x} ${n.p.y}) scale(${Math.max(s, 0.001).toFixed(3)}) translate(${-n.p.x} ${-n.p.y})`);
      });
      const g = netorbitRevealGlobal(t);
      links.forEach((l) => {
        const draw = l.n && !l.n.root ? l.n.st.draw : 1;
        l.p.setAttribute("d", noPartialPath(l.d, draw));
        l.p.style.visibility = draw <= 0 ? "hidden" : "";
      });
      nodes.forEach((n) => { if (n.lab) n.lab.style.opacity = g.labels.toFixed(3); });
      hlabels.forEach((h) => (h.style.opacity = g.labels.toFixed(3)));
      if (radar) radar.style.opacity = g.radar.toFixed(3);
      if (beam) beam.setAttribute("transform", `rotate(${noBeamDeg(t).toFixed(2)} ${C.x} ${C.y})`);
      pulses.forEach((c, i) => {
        const ph = ((t + 10) / 3 + i / 3) % 1;
        c.setAttribute("r", (38 + ph * 342).toFixed(1));
        c.style.opacity = (0.4 * (1 - ph)).toFixed(3);
      });
      if (pill) pill.style.opacity = g.pill.toFixed(3);
      if (g.header && card && card.classList.contains("revealing")) {
        card.classList.remove("revealing");
        if (chipEl) chipEl.remove();
      }
    };
    frame(0);
    this._revealing = true;
    // A newer run (replay) takes over: an older loop sees another token and leaves without touching anything.
    const run = (this._revealRun = (this._revealRun || 0) + 1);
    const t0 = performance.now();
    const step = () => {
      if (!this._revealing || this._revealRun !== run) return;
      const t = (performance.now() - t0) / 1000;
      if (t >= NO_REVEAL.end || !this.isConnected) {
        this._revealing = false;
        this._sig = "";
        if (this._hass) this.hass = this._hass;
        return;
      }
      frame(t);
      requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  // ------------------------------------------------------------------ share

  _shareTitle(hide) {
    return hide ? "My home network" : String(this._config.title || "My home network");
  }

  /** The share image as SVG text. hideNames defaults to true. Never holds addresses: see shareModel. */
  shareSvg(hideNames) {
    const model = this._model();
    if (!model.root) return "";
    const hide = hideNames !== false;
    return netorbitShareSvg(shareModel(model, { hideNames: hide }), this._shareTitle(hide));
  }

  // The share image as a PNG blob, drawn from the SVG on an offscreen canvas.
  _sharePng(hide) {
    const svg = this.shareSvg(hide);
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => {
        try {
          const scale = 1.5;
          const canvas = document.createElement("canvas");
          canvas.width = Math.round(NO_SHARE.w * scale);
          canvas.height = Math.round(NO_SHARE.h * scale);
          const ctx = canvas.getContext("2d");
          ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
          canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("Could not make the image"))), "image/png");
        } catch (e) { reject(e); }
      };
      img.onerror = () => reject(new Error("Could not draw the map"));
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(svg);
    });
  }

  // Records the first-scan animation of the share map (names hidden unless asked) to a video blob. `alive` says
  // whether the share popup is still open: once it is not (or the card has left the page) the recording stops
  // without a result and the promise rejects with an error marked `cancelled`. Every failure rejects.
  _shareVideo(hide, onTick, alive) {
    const type = noVideoType();
    if (!type) return Promise.reject(new Error("This browser can't record video"));
    const smodel = shareModel(this._model(), { hideNames: hide });
    const D = netorbitDraw(smodel, { show_ip: false }, {});
    const counts = noShareCounts(smodel);
    const total = D.scene.nodes.filter((n) => !n.hub && n.k !== "internet").length;
    const PRE = NO_REVEAL.turn; // one turn of the beam with only the router, as on a first scan
    const HOLD = 1.4;
    const phases = ["ping", "arp", "names", "snmp", "layout"];
    const frac = (t) => noClamp((t + PRE) / PRE);
    const o = {
      title: this._shareTitle(hide), counts,
      pct: (t) => frac(t) * 100,
      found: (t) => total * noEase(frac(t)),
      phase: (t) => NO_SCAN_PHASE[phases[Math.min(phases.length - 1, Math.floor(frac(t) * phases.length))]],
    };
    return new Promise((resolve, reject) => {
      let stream = null, rec = null, done = false;
      const stopTracks = () => { try { if (stream) stream.getTracks().forEach((tr) => tr.stop()); } catch (e) { /* already stopped */ } };
      const stopRec = () => { try { if (rec && rec.state !== "inactive") rec.stop(); } catch (e) { /* already stopped */ } };
      const fail = (e) => {
        if (done) return;
        done = true;
        stopTracks();
        reject(e || new Error("Recording failed"));
      };
      try {
        const canvas = document.createElement("canvas");
        canvas.width = 960;
        canvas.height = Math.round((960 * NO_SHARE.h) / NO_SHARE.w / 2) * 2; // even, for H.264
        const ctx = canvas.getContext("2d");
        netorbitPaintFrame(ctx, D.scene, -PRE, o);
        stream = canvas.captureStream(30);
        rec = new MediaRecorder(stream, { mimeType: type, videoBitsPerSecond: 5000000 });
        const chunks = [];
        rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
        rec.onstop = () => {
          if (done) return;
          done = true;
          stopTracks();
          resolve(new Blob(chunks, { type: type.split(";")[0] }));
        };
        rec.onerror = (e) => { stopRec(); fail(e && e.error); };
        rec.start(250);
        const t0 = performance.now();
        const dur = PRE + NO_REVEAL.end + HOLD;
        const step = () => {
          if (done) return;
          try {
            if (!this.isConnected || (alive && !alive())) {
              const c = new Error("Cancelled");
              c.cancelled = true;
              stopRec();
              fail(c);
              return;
            }
            const el = (performance.now() - t0) / 1000;
            netorbitPaintFrame(ctx, D.scene, Math.min(el, dur) - PRE, o);
            if (onTick) onTick(el / dur);
            if (el < dur) requestAnimationFrame(step);
            else setTimeout(() => { try { rec.stop(); } catch (e) { fail(e); } }, 120);
          } catch (e) { stopRec(); fail(e); }
        };
        requestAnimationFrame(step);
      } catch (e) { stopRec(); fail(e); }
    });
  }

  // Hands a finished image or video to the system share sheet when the browser has one for files (phones, mostly),
  // and downloads it otherwise. Resolves "shared", "cancelled" (the person closed the sheet) or "saved".
  _deliver(blob, name) {
    const save = () => { this._download(blob, name); return "saved"; };
    let file = null;
    try {
      const f = new File([blob], name, { type: blob.type });
      if (navigator.share && navigator.canShare && navigator.canShare({ files: [f] })) file = f;
    } catch (e) { file = null; }
    if (!file) return Promise.resolve(save());
    return new Promise((res) => res(navigator.share({ files: [file] }))).then(
      () => "shared",
      // AbortError: closed the sheet. Anything else (say the click is too old for the sheet to open): download.
      (e) => (e && e.name === "AbortError" ? "cancelled" : save())
    );
  }

  _download(blob, name) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
  }

  _sharePopup() {
    const card = this.shadowRoot.querySelector("ha-card");
    const old = this.shadowRoot.querySelector(".sharepop");
    if (old) { this._closeShare(); return; }
    const canCopy = !!(navigator.clipboard && navigator.clipboard.write && typeof ClipboardItem !== "undefined");
    const canVideo = !!noVideoType();
    const pop = document.createElement("div");
    pop.className = "sharepop";
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-label", "Share your map");
    pop.innerHTML = `<div class="shead"><b>Share your map</b><button class="x" aria-label="Close">&times;</button></div>` +
      (canVideo ? `<div class="seg"><button class="on" data-m="image">Image</button><button data-m="video">Animation</button></div>` : "") +
      `<img class="thumb" alt="Preview of the shared map">` +
      `<div class="trow"><div><div>Hide device names</div><div class="muted">Shows "TV 1", "Camera 2" and hub types instead.</div></div>` +
      `<button class="tg on" role="switch" aria-checked="true" aria-label="Hide device names"><i></i></button></div>` +
      `<div class="muted" style="margin-top:10px">IP and MAC addresses are never included.</div>` +
      `<div class="btns img"><button class="go dl"><ha-icon icon="mdi:download"></ha-icon>Download PNG</button>` +
      (canCopy ? `<button class="cp"><ha-icon icon="mdi:content-copy"></ha-icon>Copy</button>` : "") + `</div>` +
      (canVideo ? `<div class="btns vid" hidden><button class="go rec"><ha-icon icon="mdi:movie-open-outline"></ha-icon>Save video</button></div>` : "") +
      `<div class="status" aria-live="polite"></div>` +
      `<button class="replay">Replay scan animation</button>`;
    card.appendChild(pop);
    this._popOpen = true;
    let hide = true;
    let busy = false;
    const q = (s) => pop.querySelector(s);
    const status = (t) => (q(".status").textContent = t || "");
    const thumb = () => { q(".thumb").src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(this.shareSvg(hide)); };
    thumb();
    q(".x").addEventListener("click", () => this._closeShare());
    q(".tg").addEventListener("click", () => {
      hide = !hide;
      q(".tg").classList.toggle("on", hide);
      q(".tg").setAttribute("aria-checked", String(hide));
      thumb();
    });
    pop.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
      pop.querySelectorAll(".seg button").forEach((x) => x.classList.toggle("on", x === b));
      q(".btns.img").hidden = b.dataset.m !== "image";
      q(".btns.vid").hidden = b.dataset.m !== "video";
      status(b.dataset.m === "video" ? "Records the scan animation, about 7 seconds." : "");
    }));
    q(".dl").addEventListener("click", () => {
      this._sharePng(hide).then((b) => this._deliver(b, "netorbit-map.png")).then(() => {}, (e) => status(String(e.message || e)));
    });
    if (q(".cp")) q(".cp").addEventListener("click", () => {
      // Safari wants the clipboard write to start inside the click, with the image still on its way.
      navigator.clipboard.write([new ClipboardItem({ "image/png": this._sharePng(hide) })])
        .then(() => status("Copied. Paste it anywhere."), (e) => status("Couldn't copy: " + (e.message || e)));
    });
    if (q(".rec")) q(".rec").addEventListener("click", () => {
      if (busy) return;
      busy = true;
      q(".rec").disabled = true;
      const reset = () => { busy = false; q(".rec").disabled = false; };
      new Promise((res) => res(this._shareVideo(hide, (f) => status(`Recording... ${Math.min(100, Math.round(f * 100))}%`), () => pop.isConnected)))
        .then((b) => this._deliver(b, "netorbit-scan." + (/mp4/.test(b.type) ? "mp4" : "webm")))
        .then((r) => { reset(); status(r === "shared" ? "Shared." : r === "saved" ? "Saved." : ""); },
          (e) => { reset(); status(e && e.cancelled ? "" : "Couldn't record: " + ((e && e.message) || e)); });
    });
    q(".replay").addEventListener("click", () => this.replayScan());
  }

  _closeShare() {
    const pop = this.shadowRoot.querySelector(".sharepop");
    if (pop) pop.remove();
    if (this._popOpen) { this._popOpen = false; this._sig = ""; this.hass = this._hass; }
  }

  // Fill in the path of every map icon not resolved yet: render it once in a hidden <ha-icon>, read the path that
  // ha-svg-icon draws, cache it for the page (NO_ICON_PATHS) and copy it into every card's map.
  _resolveIcons() {
    const svg = this.shadowRoot.querySelector("svg.map");
    if (!svg) return;
    const missing = [...new Set([...svg.querySelectorAll("g.ic")].map((g) => g.dataset.icon))].filter((n) => !NO_ICON_PATHS.has(n));
    if (!missing.length) return;
    NO_ICON_CARDS.add(this);
    // One hidden box for the whole page, outside every card: a card redraws its whole shadow root, which would
    // throw away a lookup still in progress.
    let box = document.getElementById("netorbit-iconsrc");
    if (!box) {
      box = document.createElement("div");
      box.id = "netorbit-iconsrc";
      box.setAttribute("aria-hidden", "true");
      box.style.cssText = "position:absolute;width:0;height:0;overflow:hidden;visibility:hidden";
      document.body.appendChild(box);
    }
    missing.forEach((name) => {
      if (NO_ICON_PENDING.has(name)) return;
      NO_ICON_PENDING.add(name);
      const el = document.createElement("ha-icon");
      el.setAttribute("icon", name);
      box.appendChild(el);
      let tries = 0;
      const poll = () => {
        const inner = el.shadowRoot && el.shadowRoot.querySelector("ha-svg-icon");
        const path = inner && inner.shadowRoot && inner.shadowRoot.querySelector("path");
        const d = path && path.getAttribute("d");
        if (d) {
          NO_ICON_PATHS.set(name, d);
          NO_ICON_PENDING.delete(name);
          el.remove();
          NO_ICON_CARDS.forEach((card) => {
            const map = card.isConnected && card.shadowRoot && card.shadowRoot.querySelector("svg.map");
            if (!map) { NO_ICON_CARDS.delete(card); return; }
            map.querySelectorAll("g.ic").forEach((g) => {
              if (g.dataset.icon === name) g.firstElementChild.setAttribute("d", d);
            });
          });
        } else if (++tries < 50) {
          setTimeout(poll, 100);
        } else {
          NO_ICON_PENDING.delete(name);
          el.remove();
        }
      };
      poll();
    });
  }

  _wire(model) {
    const { hubs, devices } = model;
    const all = {};
    Object.values(hubs).forEach((h) => (all[h.key] = h));
    devices.forEach((d) => (all[d.key] = d));
    if (model.internet) all.internet = model.internet;
    const root = model.root;
    const col = (s) => (s === "group" ? "#64748b" : (NO_STATUS[s] || NO_STATUS.unknown).color);
    const tip = this.shadowRoot.querySelector(".tip");
    const wrap = this.shadowRoot.querySelector(".wrap");
    const svg = this.shadowRoot.querySelector("svg.map");
    const ghost = this.shadowRoot.querySelector(".ghost");
    const place = (el, ev, w, h) => {
      const r = wrap.getBoundingClientRect();
      let x = ev.clientX - r.left + 14, y = ev.clientY - r.top + 14;
      if (x + w > r.width) x -= w + 24;
      if (y + h > r.height) y -= h + 20;
      el.style.left = Math.max(4, x) + "px"; el.style.top = Math.max(4, y) + "px";
    };
    const toSvg = (ev) => {
      const pt = svg.createSVGPoint();
      pt.x = ev.clientX; pt.y = ev.clientY;
      const m = svg.getScreenCTM();
      return m ? pt.matrixTransform(m.inverse()) : { x: 0, y: 0 };
    };
    // Where a dragged device would land: a hub within reach, or a suggested hub (pink ring) when the
    // pointer is right on it. Dropping on a suggestion asks to make it a hub first. Anywhere else
    // returns null and nothing moves.
    const nearestTarget = (p, skipKey) => {
      for (const d of devices) {
        if (!d.suggested || d.key === skipKey) continue;
        const dp = this._pos[d.key];
        if (dp && Math.hypot(dp.x - p.x, dp.y - p.y) < 24) return d;
      }
      let best = null, bestD = 75;
      Object.values(hubs).forEach((h) => {
        if (h.kind !== "hub") return;
        const hp = this._pos[h.key];
        if (!hp) return;
        const d = Math.hypot(hp.x - p.x, hp.y - p.y);
        if (d < bestD) { best = h; bestD = d; }
      });
      return best;
    };

    this.shadowRoot.querySelectorAll(".node[data-key]").forEach((el) => {
      const n = all[el.dataset.key];
      if (!n) return;
      el.addEventListener("mouseenter", () => {
        if (this._drag) return;
        tip.innerHTML = this._tipHtml(n, hubs, col);
        tip.style.opacity = "1";
      });
      el.addEventListener("mousemove", (ev) => place(tip, ev, 230, 170));
      el.addEventListener("mouseleave", () => (tip.style.opacity = "0"));

      el.addEventListener("pointerdown", (ev) => {
        if (!this._edit || n.kind !== "device") return;
        ev.preventDefault();
        this._drag = { n, x0: ev.clientX, y0: ev.clientY, moved: false, id: ev.pointerId };
        el.setPointerCapture(ev.pointerId);
      });
      el.addEventListener("pointermove", (ev) => {
        const d = this._drag;
        if (!d || d.id !== ev.pointerId) return;
        if (!d.moved && Math.hypot(ev.clientX - d.x0, ev.clientY - d.y0) < 6) return;
        d.moved = true;
        tip.style.opacity = "0";
        const p = toSvg(ev);
        ghost.setAttribute("cx", p.x); ghost.setAttribute("cy", p.y);
        const h = nearestTarget(p, d.n.key);
        this.shadowRoot.querySelectorAll(".node.hot").forEach((x) => x.classList.remove("hot"));
        if (h) { const hel = this.shadowRoot.querySelector(`.node[data-key="${CSS.escape(h.key)}"]`); if (hel) hel.classList.add("hot"); }
        d.target = h;
      });
      const finish = (ev) => {
        const d = this._drag;
        if (!d || d.id !== ev.pointerId) return;
        this._drag = null;
        // Judge the drop where the pointer was released. A quick flick can end on a hub
        // without a pointermove there.
        if (ev.type === "pointerup" && Math.hypot(ev.clientX - d.x0, ev.clientY - d.y0) >= 6) {
          d.moved = true;
          d.target = nearestTarget(toSvg(ev), d.n.key);
        }
        ghost.setAttribute("cx", -500); ghost.setAttribute("cy", -500);
        this.shadowRoot.querySelectorAll(".node.hot").forEach((x) => x.classList.remove("hot"));
        if (d.moved) {
          if (d.target && d.target.suggested) this._dropOnSuggestPopup(d.target, d.n, ev, wrap, place);
          else if (d.target && d.target.key !== d.n.link) this._moveTo(d.n, d.target.key);
          else this._sig = "";
          return;
        }
        this._devicePopup(d.n, ev, wrap, place, hubs);
      };
      el.addEventListener("pointerup", finish);
      el.addEventListener("pointercancel", (ev) => { if (this._drag) { this._drag = null; ghost.setAttribute("cx", -500); } });

      el.addEventListener("click", (ev) => {
        if (this._edit && n.kind === "device") return; // handled by pointer events
        if (n.suggested && this._isAdmin()) { this._suggestPopup(n, ev, wrap, place); return; }
        if (n.kind === "new") { this._newPopup(n, ev, wrap, place); return; }
        if (n.kind === "more") { this._morePopup(n, ev, wrap, place); return; }
        if (this._edit && n.kind === "hub") { this._hubPopup(n, ev, wrap, place, n.key === root, hubs, root); return; }
        if (n.entity) this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId: n.entity }, bubbles: true, composed: true }));
      });
    });
  }

  // Zoom and pan: pinch or drag with a finger, Ctrl/Cmd + scroll wheel (a trackpad pinch), a double-tap on the
  // background, or the buttons. Only the SVG's viewBox changes. Node taps, drags and tooltips already map pointer
  // positions through the SVG's own transform, so they need no change.
  _wireZoom() {
    const wrap = this.shadowRoot.querySelector(".wrap");
    const svg = this.shadowRoot.querySelector("svg.map");
    const ctl = this.shadowRoot.querySelector(".zoomctl");
    if (!wrap || !svg) return;
    const apply = () => {
      const v = this._view || NO_VIEW;
      svg.setAttribute("viewBox", `${v.x.toFixed(1)} ${v.y.toFixed(1)} ${v.w.toFixed(1)} ${v.h.toFixed(1)}`);
      wrap.classList.toggle("zoomed", !!this._view);
      // Zoomed in (or placing devices) the map takes every touch. Otherwise one finger scrolls the page past the
      // map as it always did, and only a pinch is the map's.
      wrap.style.touchAction = this._edit || this._view ? "none" : this._config.zoom === false ? "auto" : "pan-y";
    };
    apply();
    if (this._config.zoom === false) return;

    const toMap = (x, y, inv) => { const pt = svg.createSVGPoint(); pt.x = x; pt.y = y; return pt.matrixTransform(inv); };
    const inverse = () => { const m = svg.getScreenCTM(); return m ? m.inverse() : null; };
    const zoomBy = (factor, at) => {
      const v = this._view || NO_VIEW;
      const f = at || { x: v.x + v.w / 2, y: v.y + v.h / 2 };
      this._view = netorbitZoomView(this._view, f, f, factor);
      apply();
    };
    if (ctl) {
      ctl.querySelector(".zin").addEventListener("click", () => zoomBy(1.6));
      ctl.querySelector(".zout").addEventListener("click", () => zoomBy(1 / 1.6));
      ctl.querySelector(".zfit").addEventListener("click", () => { this._view = null; apply(); });
    }
    svg.addEventListener("wheel", (ev) => {
      if (!(ev.ctrlKey || ev.metaKey)) return; // a plain wheel keeps scrolling the page
      const inv = inverse();
      if (!inv) return;
      ev.preventDefault();
      // A trackpad pinch arrives as many small deltas; a mouse wheel notch is capped to about 1.6x. Firefox can
      // report lines (1) or pages (2) instead of pixels.
      const dy = ev.deltaY * (ev.deltaMode === 1 ? 16 : ev.deltaMode === 2 ? window.innerHeight || 800 : 1);
      zoomBy(Math.exp(Math.max(-0.5, Math.min(0.5, -dy * 0.01))), toMap(ev.clientX, ev.clientY, inv));
    }, { passive: false });
    // Safari: a pinch over the map must not zoom the page as well, and two fingers must not scroll it.
    ["gesturestart", "gesturechange"].forEach((t) => svg.addEventListener(t, (ev) => ev.preventDefault()));
    svg.addEventListener("touchmove", (ev) => { if (ev.touches.length > 1 && ev.cancelable) ev.preventDefault(); }, { passive: false });

    // Fingers (or the mouse button) on the map right now, and the gesture they are making.
    const fingers = new Map();
    let g = null, lastTap = null;
    const mid = () => {
      const p = [...fingers.values()];
      return p.length > 1
        ? { x: (p[0].x + p[1].x) / 2, y: (p[0].y + p[1].y) / 2, d: Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) }
        : { x: p[0].x, y: p[0].y, d: 0 };
    };
    // (Re)start the gesture from where the fingers are now: on the first touch and whenever a finger lands or lifts.
    const begin = () => {
      const inv = inverse();
      if (!inv) { g = null; return; }
      const m = mid();
      g = { view: this._view, inv, m, anchor: toMap(m.x, m.y, inv), moved: !!(g && g.moved), pinched: !!(g && g.pinched) || fingers.size > 1, captured: !!(g && g.captured) };
    };
    svg.addEventListener("pointerdown", (ev) => {
      if (this._drag || (ev.pointerType === "mouse" && ev.button !== 0)) return; // a device is being dragged
      if (!fingers.size) this._swallowClick = false;
      fingers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      if (fingers.size > 2) return;
      begin();
      this._zooming = Date.now();
    });
    svg.addEventListener("pointermove", (ev) => {
      if (!g || !fingers.has(ev.pointerId) || this._drag) return;
      // A mouse button released just outside the map before any capture was taken never reaches us as a pointerup.
      if (ev.pointerType === "mouse" && !(ev.buttons & 1)) { end(ev); return; }
      fingers.set(ev.pointerId, { x: ev.clientX, y: ev.clientY });
      const m = mid();
      if (!g.moved && Math.hypot(m.x - g.m.x, m.y - g.m.y) < 6 && Math.abs(m.d - g.m.d) < 6) return;
      g.moved = true;
      this._zooming = Date.now();
      // Keep the events coming when the pointer leaves the map. Touches are captured by their first target already,
      // and taking that capture over would end the pan, so only a mouse is captured here.
      if (!g.captured && ev.pointerType === "mouse") { g.captured = true; try { svg.setPointerCapture(ev.pointerId); } catch (e) { /* pointer gone */ } }
      const factor = g.m.d && m.d ? m.d / g.m.d : 1;
      this._view = netorbitZoomView(g.view, g.anchor, toMap(m.x, m.y, g.inv), factor);
      apply();
    });
    const end = (ev) => {
      if (!fingers.has(ev.pointerId)) return;
      fingers.delete(ev.pointerId);
      if (fingers.size) { begin(); return; } // one finger left after a pinch: carry on panning from here
      const acted = !!(g && (g.moved || g.pinched));
      const tap = !!g && !acted && ev.type === "pointerup" && !(ev.target.closest && ev.target.closest(".node"));
      this._swallowClick = acted;
      g = null;
      this._zooming = 0;
      if (tap) {
        // A second tap on the background within 350 ms zooms in there, or back out to the whole map.
        if (lastTap && ev.timeStamp - lastTap.t < 350 && Math.hypot(ev.clientX - lastTap.x, ev.clientY - lastTap.y) < 30) {
          lastTap = null;
          const inv = inverse();
          if (this._view) { this._view = null; apply(); } else if (inv) zoomBy(2.5, toMap(ev.clientX, ev.clientY, inv));
        } else lastTap = { t: ev.timeStamp, x: ev.clientX, y: ev.clientY };
      }
      // Draw whatever changed during the gesture, or arrived while a plain tap or press was holding updates back.
      if ((acted || this._held) && this._hass) { this._held = false; this.hass = this._hass; }
    };
    svg.addEventListener("pointerup", end);
    svg.addEventListener("pointercancel", end);
    // Only the svg's own capture ending counts. A device or hub circle losing its implicit touch capture bubbles up here too.
    svg.addEventListener("lostpointercapture", (ev) => { if (ev.target === svg) end(ev); });
    // A pan or pinch is not a tap on whatever node the finger started on.
    svg.addEventListener("click", (ev) => {
      if (!this._swallowClick) return;
      this._swallowClick = false;
      ev.stopPropagation();
      ev.preventDefault();
    }, true);
  }

  _tipHtml(n, hubs, col) {
    if (n.kind === "more") {
      const counts = {};
      n.members.forEach((m) => (counts[m.status] = (counts[m.status] || 0) + 1));
      return `<b>${noEsc(n.label)}</b><div>${Object.entries(counts).map(([s, c]) => `${c} ${(NO_STATUS[s] || NO_STATUS.unknown).label.toLowerCase()}`).join(", ")}</div><div style="color:#94a3b8">Tap to list them. Devices with problems are always shown on the map.</div>`;
    }
    if (n.kind === "internet") {
      const what = n.status === "online" ? "Public DNS servers are answering."
        : n.status === "stale" ? "Your router answers, but public DNS servers don't. Your internet connection looks down."
        : "NetOrbit can't tell right now: your router isn't answering either, or the check just started.";
      return `<b>Internet</b><div><span class="k">Status</span><span style="color:${col(n.status)}">${n.status === "online" ? "Up" : n.status === "stale" ? "Down" : "Unknown"}</span></div>` +
        `<div>${what}</div>` + (n.status === "online" && n.last_seen ? `<div><span class="k">Came up</span>${noRel(n.last_seen)}</div>` : "");
    }
    if (n.kind === "newhub") return `<b>New devices</b><div>Found on the network but not tracked yet, plus anything that might be a hub. Tap one to decide.</div>`;
    if (n.kind === "group") return `<b>${noEsc(n.label)}</b><div>NetOrbit does not know which access point or switch these use yet. Use Edit layout, the NetOrbit settings, or an AI assistant to place them.</div>`;
    const via = n.kind === "hub" ? (hubs[n.parent] ? hubs[n.parent].label : "") : (hubs[n.link] && hubs[n.link].kind === "hub" ? hubs[n.link].label : "");
    return `<b>${noEsc(n.label)}</b>` +
      (n.suggested ? `<div style="color:${NO_SUGGEST.light}">Might be a ${noEsc((n.suggested.type || "hub").replace("_", " "))}: ${noEsc((n.suggested.reasons || []).join(", "))}</div>` : "") +
      `<div><span class="k">Status</span><span style="color:${col(n.status)}">${(NO_STATUS[n.status] || NO_STATUS.unknown).label}</span>${n.sleepy ? " · sleeps" : ""}</div>` +
      (n.ip ? `<div><span class="k">IP</span>${noEsc(n.ip)}</div>` : "") +
      (n.mac ? `<div><span class="k">MAC</span>${noEsc(n.mac)}</div>` : "") +
      (n.vendor ? `<div><span class="k">Maker</span>${noEsc(n.vendor)}</div>` : "") +
      (n.hostname && n.hostname !== n.label ? `<div><span class="k">Host</span>${noEsc(n.hostname)}</div>` : "") +
      (via ? `<div><span class="k">Via</span>${noEsc(via)}${n.port != null ? ` · port ${noEsc(n.port)}` : ""}</div>` : "") +
      (n.network || n.vlan != null ? `<div><span class="k">Network</span>${noEsc(n.network || "")}${n.network && n.vlan != null ? " · " : ""}${n.vlan != null ? `VLAN ${noEsc(n.vlan)}` : ""}</div>` : "") +
      (n.kind === "device" && n.placement ? `<div><span class="k">Placed</span>${noEsc(NO_PLACEMENT[n.placement] || n.placement)}</div>` : "") +
      `<div><span class="k">Last seen</span>${n.status === "online" ? "now" : noRel(n.last_seen)}</div>` +
      this._dhcpTip(n);
  }

  _dhcpTip(n) {
    const c = n.ip_changes || 0;
    if (n.kind === "hub" && c >= 1) {
      return `<div class="tiptip">Its IP has changed ${c} time${c > 1 ? "s" : ""}. NetOrbit follows it by MAC address, but network gear works best with a DHCP reservation in your router.</div>`;
    }
    if (n.kind === "device" && c >= 2) {
      return `<div class="tiptip">Its IP has changed ${c} times. NetOrbit keeps your settings by MAC address, so nothing is lost. A DHCP reservation keeps the address steady if other things rely on it.</div>`;
    }
    return "";
  }

  // Move a device to a hub: draw it there straight away, then tell Home Assistant.
  _moveTo(n, hubKey) {
    if (!this._optimistic) this._optimistic = new Map();
    this._optimistic.set(n.key, { link: hubKey, t: Date.now() });
    const redraw = () => { this._sig = ""; if (this._hass) this.hass = this._hass; };
    this._call("assign_device", { device: [this._ref(n)], hub: hubKey.replace(/^hub:/, "") }).then((ok) => {
      // If the save failed, put the device back now; if the new placement never shows up, in 10 s.
      if (!ok) { this._optimistic.delete(n.key); redraw(); } else setTimeout(redraw, 10500);
    });
    this._sig = "";
    this.hass = this._hass;
  }

  _ref(n) {
    return (n.raw && (n.raw.mac || n.raw.ip)) || n.mac || n.ip || n.label;
  }

  _call(service, data) {
    const entryId = this._summaryEntry();
    if (entryId) data.config_entry_id = entryId;
    this._sig = "";
    return this._hass.callService("netorbit", service, data).then(() => true, (e) => {
      console.error("NetOrbit:", e);
      this.dispatchEvent(new CustomEvent("hass-notification", { detail: { message: `NetOrbit: ${e.message || e}` }, bubbles: true, composed: true }));
      return false;
    });
  }

  _openPop(html, ev, wrap, place, w = 250, h = 200) {
    this._closePop(wrap);
    const pop = document.createElement("div");
    pop.className = "pop";
    pop.innerHTML = html;
    wrap.appendChild(pop);
    place(pop, ev, w, h);
    this._popOpen = true;
    pop.querySelectorAll(".x").forEach((b) => b.addEventListener("click", () => this._closePop(wrap)));
    return pop;
  }

  _closePop(wrap) {
    const old = wrap.querySelector(".pop");
    if (old) old.remove();
    if (this._popOpen) { this._popOpen = false; this._sig = ""; this.hass = this._hass; }
  }

  _hubOptions(hubs, current) {
    const real = hubs[current] && hubs[current].kind === "hub";
    return (real ? "" : `<option value="" selected disabled>Choose a hub…</option>`) +
      Object.values(hubs).filter((h) => h.kind === "hub")
        .sort((a, b) => String(a.label).localeCompare(String(b.label)))
        .map((h) => `<option value="${noEsc(h.key.replace(/^hub:/, ""))}" ${h.key === current ? "selected" : ""}>${noEsc(h.label)}</option>`).join("");
  }

  _devicePopup(n, ev, wrap, place, hubs) {
    const types = Object.entries(NO_HUB_TYPE).filter(([k]) => k !== "router").map(([k, v]) => `<option value="${k}" ${k === "access_point" ? "selected" : ""}>${v}</option>`).join("");
    const pop = this._openPop(
      `<b>${noEsc(n.label)}</b><div class="muted">${noEsc(NO_PLACEMENT[n.placement] || "")}</div>` +
      `<label>Connects through<select class="hub">${this._hubOptions(hubs, n.link)}</select></label>` +
      `<div class="row"><button class="go move">Move</button>${n.placement === "manual" ? `<button class="reset">Automatic</button>` : ""}</div>` +
      `<label>Or this device is itself a<select class="type">${types}</select></label>` +
      `<div class="row"><button class="mk">Make it a hub</button><button class="x">Close</button></div>`,
      ev, wrap, place, 250, 260
    );
    const q = (s) => pop.querySelector(s);
    q(".move").addEventListener("click", () => {
      const hub = q(".hub").value;
      if (!hub) { q(".hub").focus(); return; }
      this._closePop(wrap);
      this._moveTo(n, "hub:" + hub);
    });
    if (q(".reset")) q(".reset").addEventListener("click", () => { this._closePop(wrap); this._call("reset_placement", { device: [this._ref(n)] }); });
    q(".mk").addEventListener("click", () => { const t = q(".type").value; this._closePop(wrap); this._call("confirm_hub", { device: this._ref(n), type: t }); });
  }

  // Hubs a hub can hang off: the router and every other real hub, except itself and the hubs below it.
  _parentOptions(n, hubs, root) {
    const below = (h) => {
      let cur = h, guard = 0;
      while (cur && guard++ < 100) {
        if (cur.key === n.key) return true;
        cur = hubs[cur.parent];
      }
      return false;
    };
    const list = Object.values(hubs).filter((h) => h.kind === "hub" && !below(h))
      .sort((a, b) => (a.key === root ? -1 : b.key === root ? 1 : String(a.label).localeCompare(String(b.label))));
    const now = hubs[n.parent] && list.indexOf(hubs[n.parent]) >= 0 ? n.parent : root;
    // Two hubs with the same name get their address added, so they can be told apart.
    const seen = {};
    list.forEach((h) => { const k = String(h.label).toLowerCase(); seen[k] = (seen[k] || 0) + 1; });
    const text = (h) => (seen[String(h.label).toLowerCase()] > 1 ? `${h.label} (${h.ip || h.key.replace(/^hub:/, "")})` : h.label);
    return { now, html: list.map((h) => `<option value="${noEsc(h.key.replace(/^hub:/, ""))}" ${h.key === now ? "selected" : ""}>${noEsc(text(h))}</option>`).join("") };
  }

  _hubPopup(n, ev, wrap, place, isRoot, hubs, root) {
    const cur = NO_HUB_TYPE[n.hub_type] ? n.hub_type : "other";
    const par = isRoot ? null : this._parentOptions(n, hubs, root);
    const types = Object.entries(NO_HUB_TYPE).filter(([k]) => k !== "router" || isRoot)
      .map(([k, v]) => `<option value="${k}" ${k === cur ? "selected" : ""}>${v}</option>`).join("");
    const pop = this._openPop(
      `<b>${noEsc(n.label)}</b><div class="muted">${noEsc(NO_HUB_TYPE[n.hub_type] || "Hub")}</div>` +
      `<label>Name<input class="name" value="${noEsc(n.label)}"></label>` +
      (isRoot ? "" : `<label>Type<select class="type">${types}</select></label>`) +
      (isRoot ? "" : `<label>Connected to<select class="parent">${par.html}</select></label>`) +
      `<div class="row"><button class="go save">Save</button>${isRoot ? "" : `<button class="warn rm">Remove</button>`}<button class="x">Close</button></div>`,
      ev, wrap, place, 250, isRoot ? 160 : 270
    );
    const q = (s) => pop.querySelector(s);
    q(".save").addEventListener("click", () => {
      const name = q(".name").value.trim();
      const type = q(".type") ? q(".type").value : cur;
      const parent = q(".parent") ? q(".parent").value : "";
      this._closePop(wrap);
      const data = { hub: n.key.replace(/^hub:/, "") };
      if (name && name !== n.label) data.name = name;
      if (type !== cur) data.type = type;
      if (par && parent && "hub:" + parent !== par.now) data.parent = parent;
      if (data.name || data.type || data.parent) this._call("update_hub", data);
    });
    if (q(".rm")) q(".rm").addEventListener("click", () => { this._closePop(wrap); this._call("remove_hub", { hub: n.key.replace(/^hub:/, "") }); });
  }

  _suggestPopup(n, ev, wrap, place) {
    const s = n.suggested;
    const pop = this._openPop(
      `<b>${noEsc(n.label)}</b><div class="muted">NetOrbit thinks this is a ${noEsc((s.type || "hub").replace("_", " "))} because it is ${noEsc((s.reasons || []).join(", "))}.</div>` +
      `<div class="row"><button class="go yes">It's a hub</button><button class="no">Not a hub</button></div><div class="row"><button class="x">Close</button></div>`,
      ev, wrap, place, 250, 170
    );
    const q = (sel) => pop.querySelector(sel);
    q(".yes").addEventListener("click", () => { this._closePop(wrap); this._call("confirm_hub", { device: s.key, type: s.type || "access_point" }); });
    q(".no").addEventListener("click", () => { this._closePop(wrap); this._call("dismiss_hub", { device: s.key }); });
  }

  // A device was dropped on a suggested hub: confirm it as a hub and move the device under it in one step.
  _dropOnSuggestPopup(target, moving, ev, wrap, place) {
    const s = target.suggested;
    const want = s.type && NO_HUB_TYPE[s.type] ? s.type : "access_point";
    const types = Object.entries(NO_HUB_TYPE).filter(([k]) => k !== "router").map(([k, v]) => `<option value="${k}" ${k === want ? "selected" : ""}>${v}</option>`).join("");
    const pop = this._openPop(
      `<b>Make ${noEsc(target.label)} a hub?</b><div class="muted">${noEsc(moving.label)} will be placed under it.</div>` +
      `<label>It is a<select class="type">${types}</select></label>` +
      `<div class="row"><button class="go yes">Make hub and move</button><button class="x">Cancel</button></div>`,
      ev, wrap, place, 260, 170
    );
    const q = (sel) => pop.querySelector(sel);
    q(".yes").addEventListener("click", () => {
      const t = q(".type").value;
      this._closePop(wrap);
      this._call("confirm_hub", { device: s.key, type: t, assign: [this._ref(moving)] });
    });
  }

  _newPopup(n, ev, wrap, place) {
    const admin = this._isAdmin();
    const pop = this._openPop(
      `<b>${noEsc(n.label)}</b><div class="muted">${noEsc(n.ip || "")} ${noEsc(n.vendor || "")}</div>` +
      (admin
        ? `<div class="row"><button class="go track">Track</button><button class="ign">Ignore</button><button class="x">Close</button></div>`
        : `<div class="muted" style="margin-top:8px">An admin can track or ignore this device.</div><div class="row"><button class="x">Close</button></div>`),
      ev, wrap, place, 250, 120
    );
    const q = (s) => pop.querySelector(s);
    if (q(".track")) q(".track").addEventListener("click", () => { this._closePop(wrap); this._call("track_device", { mac: n.raw.mac || undefined, ip: n.raw.ip || undefined }); });
    if (q(".ign")) q(".ign").addEventListener("click", () => { this._closePop(wrap); this._call("ignore_device", { mac: n.raw.mac || undefined, ip: n.raw.ip || undefined }); });
  }

  _morePopup(n, ev, wrap, place) {
    const rows = n.members.map((m) => `<div class="mrow" data-e="${noEsc(m.entity || "")}"><i style="background:${(NO_STATUS[m.status] || NO_STATUS.unknown).color}"></i>${noEsc(m.label)}<span>${noEsc(m.ip || "")}</span></div>`).join("");
    const pop = this._openPop(`<b>${noEsc(n.label)}</b><div class="mlist">${rows}</div><div class="row"><button class="x">Close</button></div>`, ev, wrap, place, 260, 340);
    pop.querySelectorAll(".mrow[data-e]").forEach((r) => r.addEventListener("click", () => {
      if (!r.dataset.e) return;
      this._closePop(wrap);
      this.dispatchEvent(new CustomEvent("hass-more-info", { detail: { entityId: r.dataset.e }, bubbles: true, composed: true }));
    }));
  }

  _summaryEntry() {
    const s = this._entities().find((x) => x.attributes.kind === "summary");
    return s ? s.attributes.config_entry_id : undefined;
  }
}

// Define the card in whatever registry window.customElements is right now. Home Assistant 2026.10 and older
// can run this file before the app swaps in its scoped custom element registry, which hides a define made
// earlier ("Custom element doesn't exist"). Defining again once the app's own <home-assistant> element is
// defined puts the card in the registry the app actually uses.
function noDefine() {
  const reg = window.customElements;
  if (!reg.get("netorbit-card")) reg.define("netorbit-card", NetOrbitCard);
}
noDefine();
if (window.customElements.whenDefined) {
  window.customElements.whenDefined("home-assistant").then(noDefine).catch((e) => console.warn("NetOrbit: card define failed", e));
}
window.customCards = window.customCards || [];
if (!window.customCards.find((c) => c.type === "netorbit-card")) {
  window.customCards.push({
    type: "netorbit-card",
    name: "NetOrbit",
    description: "Live radial map of your network from the NetOrbit integration.",
    preview: false,
    documentationURL: "https://github.com/the-scott-davis/netorbit",
  });
}
if (typeof module !== "undefined") module.exports = { NetOrbitCard, netorbitLayout, noGroupFor, noHeight, netorbitZoomView, netorbitScanMode, netorbitScanText, shareModel, netorbitShareSvg, netorbitDraw, netorbitRevealNode, netorbitRevealGlobal, noPartialPath, noShareType };
console.info("%c NETORBIT %c " + NETORBIT_VERSION, "color:#0b1220;background:#34d399;font-weight:700", "color:#34d399");
