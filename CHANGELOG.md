# Changelog

## Unreleased

### Changed
- **Faster startup.** Home Assistant no longer waits for NetOrbit's network scan when it starts, so the "Waiting for integrations to complete setup: netorbit" message is gone. NetOrbit draws the map from what it saved and pings those devices, then runs the full scan (ping sweep, ARP, names, SNMP) in the background and updates the map when it's done. The hub tree from the last scan is saved too, so the map looks the same right after a restart.
- A brand new install finishes setup right away and fills the map once the first scan is done.

### Fixed
- The Home Assistant host itself now shows on the map (#10). A machine never has an ARP entry for its own address, so it was never found. NetOrbit now adds the host from Home Assistant's network adapters when an adapter address is inside the watched subnet (a container on a Docker bridge network is skipped). It is named "Home Assistant" unless a hostname is known and gets the Home Assistant icon. The first scan after upgrading reports the host as a new device once. If you added the host by hand, that entry is kept and not doubled.

### Added
- **UniFi hub tree and wired placement.** NetOrbit reads Home Assistant's own `unifi` integration (no extra login). UniFi switches and access points become hubs under the device they plug into, and every client is placed on its switch (wired) or access point (Wi-Fi), including USW Flex switches that have no SNMP. The switch port shows in the hover details. A device you placed by hand stays where you put it.
- **Zoom and pan the map.** Pinch, double-tap or Ctrl/Cmd+scroll to zoom, drag to look around, or use the + and - buttons (thanks @xlntech1, #12). Turn it off with the card option `zoom: false`.
- **Card `height` takes any CSS length**, like `80vh` or `calc(100vh - 180px)`, so the map can fill a Panel view (thanks @ninchistudios, #11). Plain numbers still mean pixels.
- **First-scan animation on the card.** While the first scan on a new install runs, the card shows a radar sweeping around the router, a "Scanning N%" chip, and a pill at the bottom with how many devices it has found and what it's doing, updated live. When the map arrives it plays in once: the beam turns, hubs and devices pop in as it passes, links draw in, then the labels and the normal header come back. Later scans only add a small "Scanning..." chip before Edit layout. With reduced motion turned on, there's no beam or pulsing, the range rings stay still and the map fades in.
- **Share your map.** A Share button in the card header makes a PNG of the map (download, or copy where the browser allows) or a short video of the scan animation (mp4 where the browser can record it, otherwise webm). On phones and other browsers with a share sheet, the file opens in it instead of downloading. "Hide device names" is on by default: devices become "TV 1", "Camera 2" and so on, hubs show only their type, and the title becomes "My home network". IP and MAC addresses are never included, and devices waiting to be added and suggested hubs are left out. Turn the button off with the card option `show_share: false`.
- "Replay scan animation" in the Share popup plays the reveal again.
- Move a hub under another hub from the card: in Edit layout, tap the hub and pick Connected to.
- Scan progress on `sensor.netorbit_devices_online`: `scanning`, `scan_phase`, `scan_progress`, `found` and `first_scan_done`. They change a few times per scan, for the first scan and for the regular ones, and stay out of the history database (`first_scan_done` aside).

### Fixed
- A mouse button released just outside the map no longer leaves the map panning whenever the mouse moves over it.
- A Home Assistant update that arrives while you tap or press the map is drawn when you let go. Before, it waited for the next update.
- Ctrl+scroll zooms at the same speed in Firefox, which reports scroll in lines.
- Hubs with no IP (an unmanaged switch, or an access point in bridge mode) no longer turn red once the "not seen" window passes. NetOrbit can't ping them, so they now take their status from the devices behind them: green while anything behind them answers, red only once nothing behind them has been seen for the window, grey when nothing is behind them. Before, they went red (and fired `netorbit_device_stale`) a day after being added, whatever was behind them.

## 0.2.8

### Added
- **Internet node.** An Internet globe sits just above the router. Each ping round also pings two public DNS servers (1.1.1.1 and 8.8.8.8). It turns red ("Internet down") when neither answers for two rounds in a row while the router still answers, and grey when the router isn't answering either. Comes with `sensor.netorbit_internet` and `netorbit_internet_down` / `netorbit_internet_up` events. On by default; turn it off under Settings ("Show the internet on the map"), or hide just the node with the card option `show_internet: false`.

### Changed
- Devices you placed by hand are easier to spot in Edit layout: their circle fills blue and gets a pin badge, while the outline keeps its green, yellow or red status. Before, it was a small blue dot.

### Fixed
- You can change a hub's type from the card: in Edit layout, tap the hub and pick a Type (Switch, Access point, Mesh node or Hub). Before, the hub menu only offered rename and remove.
- 0.2.7 could move a wireless device to the router after a restart, when the switch next to the router had forgotten it but a switch further down still listed it on its uplink. Only switches plugged straight into the router count as proof that a device is wired to the router now; otherwise the device keeps its last placement.

## 0.2.7

### Fixed
- Making a device a hub (or removing a hub) places the devices behind it straight away. Before, the map waited for the next network scan, up to 5 minutes. NetOrbit now works the layout out again from the switch tables it already has.
- With SNMP, a device the switches only ever see on the port facing the router (plugged into the router itself, into an unmanaged switch on it, or on the router's own Wi-Fi) is placed under the router. Before, it stayed unplaced.

## 0.2.6

### Changed
- Moving a device is instant. After a drag (or Move to), the device is drawn under its new hub right away with a spinning blue "saving" ring until Home Assistant confirms. Before, nothing happened on screen for a second or two.
- Any change from the card, a service or an AI assistant redraws the map straight away from what NetOrbit already knows, and checks the network afterwards in the background. Before, every change waited for a full ping round, which takes a couple of seconds when some devices don't answer.

## 0.2.5

### Changed
- Dragging a device onto a suggested hub (the spinning pink ring) now asks "Make it a hub?" and, if you say yes, makes it a hub and moves the device under it in one step. Before, suggested hubs ignored the drop.
- The `confirm_hub` service takes an optional `assign` list of devices to place under the new hub.

## 0.2.4

### Fixed
- Map icons on Safari and in the iOS/iPadOS companion app sat away from their circles, like a second ring of loose icons. Icons are now drawn as part of the map itself, so every browser puts them in the same place.

Thanks to @mgeorgieff for tracking this down and fixing it in #2.

## 0.2.3

### Fixed
- On Home Assistant 2026.10 and older, the card could fail to load: the card picker showed a tile that spun forever, and a NetOrbit card said "Custom element doesn't exist". The card script sometimes loaded a moment before Home Assistant was ready and got lost. It now registers itself again once Home Assistant is ready.

### Changed
- When setup finishes, NetOrbit tells you the one remaining step: add the NetOrbit card to a dashboard. The README has a new "Add the map to a dashboard" section, including a full-screen Panel view.

## 0.2.2

### Security
- AI assistant tools that change the map now require a Home Assistant admin. Reading the map is still open to the assistant. Voice satellites and other callers with no user can't make changes.
- The assistant prompt tells the model to treat device names as data and never act on instructions inside them, since any device can pick its own name.
- Your custom SNMP community is only sent to hubs, suggested hubs and network-gear makers, not to every device on the network. SNMP v2c sends it unencrypted. Gear suggested during a scan is asked in the same scan.

Thanks to @ninchistudios for the review in #1.

## 0.2.1

### Fixed
- Drag and drop now judges the drop where you let go, so a quick flick onto a hub works.
- Google and Nest devices get an icon that exists (one was blank).
- Hub labels no longer overlap each other on busy maps.
- After an update, browsers load the new card right away instead of a cached copy.
- Uses the current Home Assistant field for a device's integration (the old one is deprecated and logged a warning on 2026.10).

## 0.2.0

First public release.

### Added
- Ready for the Home Assistant Marketplace (2026.11 and newer) as well as HACS: brand icon shipped with the integration, Marketplace categories, install docs for both.
- Hub detection: obvious network gear is added for you, likely gear is suggested with a one-tap confirm or dismiss.
- SNMP: reads managed switch MAC address tables to place wired and wireless devices automatically. Community is configurable and redacted from diagnostics.
- Controller placement from UniFi and Netgear/Orbi "connected AP" data.
- Three ways to place devices by hand: drag and drop on the card, paste a client list, or an AI assistant through Home Assistant's MCP Server and Assist.
- DHCP handling: everything is keyed by MAC, hubs follow their MAC to new IPs, devices added by IP switch to their MAC, IP change counts with DHCP reservation tips on the card.
- `netorbit.export_config` and `netorbit.import_config` to back up and restore your setup.
- Large networks fold quiet devices into "+N more" bubbles (`max_devices`, default 80). Problem devices and possible hubs always stay on the map.
- Devices are grouped by type until NetOrbit knows where they connect, using the name or, failing that, the maker.
- Names come from Home Assistant's device list (skipping entries that are just a MAC), device trackers, Ping sensors you already set up, the router's local DNS, and Bonjour.
- Possible hubs have their own color (pink), so they never look like "seen recently".
- Network simulator and browser tests covering hundreds of generated home networks.

### Changed
- Needs Home Assistant 2026.2 or newer.

## 0.1.0

- Initial version: ping sweep, ARP, radial card, events and blueprints.
