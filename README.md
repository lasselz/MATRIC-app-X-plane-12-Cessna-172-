# X-Plane 12 → MATRIC Cockpit Bridge

A single-file Python script that reads live cockpit data from X-Plane 12 over UDP and mirrors it, in real time, onto a [MATRIC](https://matricapp.com/) deck — plus a live text dashboard in the console itself, no MATRIC required to see it working.

**Version:** 0.77
**Script:** `XPlane12_MATRIC_Bridge_v0.77.py`

---

## Contents

- [Short resume](#short-resume)
- [Installation](#installation)
- [Setting up the MATRIC deck](#setting-up-the-matric-deck)
- [Running alongside X-Plane](#running-alongside-x-plane)
- [Code walkthrough](#code-walkthrough)
- [Appendix A — full dataref reference](#appendix-a--full-dataref-reference)
- [Appendix B — MATRIC control reference](#appendix-b--matric-control-reference)

---

## Short resume

This script subscribes to a set of X-Plane **datarefs** — electrical, engine, lighting, flight controls, fuel, annunciators, GPS/FMS, FSEconomy status, and the aircraft's tail number — over X-Plane's built-in UDP interface, and:

- Renders a flicker-free live dashboard in the console window.
- Pushes every change to a MATRIC deck (buttons, sliders, a multi-position switch, and two text-driven buttons) via MATRIC's local Integration API.
- Can **write** a dataref back to X-Plane: an optional auto-pause feature that stops the sim a set distance from the next waypoint.

Key characteristics:

- **No third-party Python packages.** Only the standard library (`json`, `socket`, `struct`, `sys`, `time`, `os`, `threading`), plus the built-in `msvcrt` on Windows for hotkeys.
- **Diff-based MATRIC traffic.** Only changed values are pushed each cycle, not a constant stream.
- **F1** forces a full resync to MATRIC regardless of what changed (useful if MATRIC and the sim ever visibly drift apart).
- **F2** toggles the auto-pause-near-waypoint feature (starts OFF).
- **Self-healing connection.** If X-Plane closes and reopens, the script recovers on its own — no restart needed, and the console shows an explicit "Awaiting connection" screen instead of frozen/stale data in the meantime.

> This is a living document — built up incrementally alongside the script, one dataref/control at a time. A few choices (thresholds, specific mappings, port numbers) are tuned for one aircraft/deck and are called out as such below.

---

## Installation

### 1. Install Python

Needs Python 3.8+ (only uses f-strings and standard-library features).

1. Download the latest Python 3 installer from [python.org/downloads](https://www.python.org/downloads/).
2. Run it, and **tick "Add python.exe to PATH"** before clicking Install.
3. Confirm it worked from Command Prompt:
   ```
   python --version
   ```
   If it says "not recognized", Python wasn't added to PATH — re-run the installer and use "Modify" to fix it.

### 2. Get the script

Save `XPlane12_MATRIC_Bridge_v0.77.py` anywhere convenient on the PC running X-Plane (e.g. `C:\XPlaneTools\`). No installer, no build step — it's one plain `.py` file.

### 3. Dependencies

None. Nothing to `pip install`.

> On non-Windows systems the dashboard and X-Plane reading still work, but F1/F2 and the MATRIC pairing config path are Windows-specific (MATRIC itself is a Windows/tablet app).

---

## Setting up the MATRIC deck

### 3.1 Get MATRIC

Download and install MATRIC from [matricapp.com](https://matricapp.com/), and set up the companion tablet/device app. Get the PC app and the deck device talking to each other first, independent of this script.

### 3.2 Enable the Integration API

Off by default:

1. Close MATRIC.
2. Open `%USERPROFILE%\Documents\.matric\config.json`.
3. Set `"EnableIntegrationAPI": true`.
4. Save and restart MATRIC.

### 3.3 Build a deck with matching control names

Every value is addressed by name, so the deck needs controls **named exactly** as in [Appendix B](#appendix-b--matric-control-reference). Short version by control type:

| Type | Names |
|---|---|
| 15 on/off buttons | `BATTERY`, `ALTERNATOR`, `AVIONICS`, `CROSSTIE`, `FUELPUMP`, `BEACON`, `LANDING_LIGHT`, `TAXI_LIGHT`, `NAV_LIGHT`, `STROBE_LIGHT`, `PITOT_HEAT`, `AP_SERVO`, `PARK_BRAKE`, `VAC_WARNING`, `VOLTS_WARNING`, `OIL_WARNING` |
| 4 mutually-exclusive flap buttons | `FLAPS_UP`, `FLAPS_10`, `FLAPS_20`, `FLAPS_FULL` |
| 3 sliders (0–100) | `ELEV_TRIM`, `MIXTURE`, `THROTTLE` |
| 1 five-position switch | `MAGNETOS_STARTER` |
| 3 text/composite buttons | `FUEL_WARNING` (on/off + dynamic text), `CALLSIGN` (text only), `FSEconomy` (on/off/flashing) |

### 3.4 First run: pairing

1. Run the script from a **Command Prompt** the first time (not by double-clicking — see [§4.3](#43-running-the-script)).
2. MATRIC shows an **Authorization Required** popup containing a PIN.
3. Type that PIN into the console when asked, and press Enter.

The PIN is cached in a `.matric_pin` file next to the script — every run after that (including double-clicking) reuses it automatically.

---

## Running alongside X-Plane

### 4.1 X-Plane network settings

No special configuration needed when both run on the **same PC** (the script talks to `127.0.0.1:49000`, X-Plane's default UDP port). Worth checking in X-Plane's `Settings → Network`:

- Confirm the UDP port is **49000**. If changed, update `XP_PORT` in the script.
- Running across **different machines**? Set `XP_IP` to X-Plane's LAN IP, and allow UDP on ports 49000/49003 through the firewall.

No dataref-specific setup needed on the X-Plane side — the script subscribes to everything itself, at startup.

### 4.2 Start order doesn't matter

The script can start before or after X-Plane, and X-Plane can be closed and reopened at any time. If no data is arriving, the console shows an "Awaiting connection to X-Plane 12…" screen and switches back automatically — with a full resync to MATRIC — the moment X-Plane responds again. See [§5.7](#57-the-main-loop-change-detection-f1f2-and-reconnection).

### 4.3 Running the script

- **First run (pairing):** Command Prompt → `cd` into the script's folder → `python XPlane12_MATRIC_Bridge_v0.77.py`. A real console is required to type the PIN in.
- **Every run after:** double-clicking works fine, since the PIN is cached.
- **While running:** `F1` forces a full MATRIC resync · `F2` toggles auto-pause-near-waypoint · `Ctrl+C` exits cleanly.

---

## Code walkthrough

### 5.1 Dataref registry and state storage

`DATAREFS` is one dictionary mapping a small integer ID to the exact dataref path (as bytes — it goes straight into a binary packet). Everything else keys off these integers rather than the long dataref strings.

Two dictionaries mirror it: `raw_states` (untouched last value per ID) and `shared_states` (the "logical" value used for display/MATRIC — identical to raw for most, but debounced for two noisy ones, see [§5.3](#53-boolean-vs-continuous-values-and-the-debounce-filter)). Both are built from `DATAREFS.keys()` directly, so adding a dataref can't fall out of sync with storage.

Three groups of datarefs are **string values**, which X-Plane's UDP interface can't send directly (it only sends floats) — each is subscribed one character at a time and reassembled:

| String value | IDs | Length |
|---|---|---|
| `acf_tailnum` (aircraft tail number) | 28–67 | 40 chars |
| `gps_nav_id` (active-leg waypoint ID) | 70–77 | 8 chars (headroom; real IDs are usually ≤5) |

### 5.2 The network thread

`XPlaneNetworkThread` is a daemon thread owning one UDP socket. On start (and periodically while no data arrives), it sends one pre-built "RREF" subscription packet per dataref, asking X-Plane to stream that value at 30 Hz.

Each incoming packet can batch **many** datarefs at once. The parser reads them all into a local dict first, *without* holding the lock, then takes `state_lock` once to apply the whole batch — avoiding repeated lock acquisition per packet.

**Connection resilience:**

- `last_data_received` (only updated on an actual packet — the true connection signal) is tracked separately from `last_subscribe_attempt` (only throttles retry frequency). Keeping these separate matters: if a retry attempt bumped the same clock the main loop checks, the dashboard would wrongly think it was still connected.
- On Windows, a UDP socket can raise a `ConnectionResetError` on the *next* `recvfrom()` after X-Plane closes (the OS delivers an ICMP "port unreachable" from an earlier send as an error on the following receive). This is caught explicitly and treated like a timeout — without it, the thread would die silently the first time X-Plane closes.

### 5.3 Boolean vs. continuous values, and the debounce filter

Most switches are simple: store the raw float, `> 0.5` means ON. A few datarefs need different handling, tracked in dedicated sets so the parser branches correctly: `RATIO_DATAREFS` (continuous 0–1 or −1–1 values), `FUEL_QTY_DATAREF_IDS` and `GPS_DME_DIST_DATAREF_IDS` (raw numeric), the tailnum/waypoint character-ID sets, and a few individually discrete ones (`IGNITION_KEY_ID`, `YOKE_YAW_ID`, `HSI_SELECTOR_ID`).

Two datarefs (`FILTERED_DATAREFS` — the alternator and fuel pump) are **debounced**: a state change only takes effect after `REQUIRED_READINGS` (5) consecutive readings agree, filtering brief sensor flicker.

### 5.4 The dashboard renderer

`build_dashboard()` returns one string for the whole screen, written with a single `sys.stdout.write()`. Two ANSI tricks keep it flicker-free:

- `\033[H` moves the cursor to top-left instead of clearing + repainting — the layout never changes shape.
- `\033[K` (erase-to-end-of-line) on every line before its newline, so a shorter new value (e.g. "ON") fully overwrites a longer old one (e.g. "OFF") rather than leaving stray characters.

The main loop only redraws (and only pushes to MATRIC) when the state actually differs from last time — compared at *display* precision via `rounded_for_comparison()`, not raw floats, so sub-thousandth sensor jitter can't force a redraw every 100 ms.

When no data has arrived for `CONNECTION_TIMEOUT_SEC` (3s), `build_awaiting_screen()` replaces the dashboard entirely, using the same overwrite technique.

### 5.5 Derived/composite states

A few MATRIC controls aren't a straight 1:1 dataref mirror:

- **Flaps** — `active_flap_button()` compares the flap ratio against 4 target positions and lights whichever is closest; the other three are always sent as off in the same push, so exactly one is ever lit mid-transition.
- **Sliders** — `elev_trim_slider_value()` / `unit_ratio_slider_value()` map a ratio onto MATRIC's 0–100 scale (trim is inverted: −1.0→100, 0→50, +1.0→0).
- **Ignition switch** — `ignition_key_switch_position()` clamps 0–4 and adds a configurable base offset, since MATRIC's position numbering has changed between versions (0-based vs 1-based).
- **FUEL_WARNING** — `fuel_warning_state()` compares both tanks' kg readings against `FUEL_LOW_THRESHOLD_KG` (10.0 kg) and returns both an activation flag and which text variant applies (`"L FUEL R"` / `"L FUEL   "` / `"   FUEL R"`).
- **CALLSIGN** — `read_tail_number()` reassembles the 40-character tail number.
- **FSEconomy** — `fse_button_desired_state()` returns solid-on (connected + flying), flashing at 1 Hz (connected, not flying), or off (not connected) — see [§5.6](#56-fseconomy-status-and-sim-pause-write).
- **Next Waypoint / Distance to WPT** — `read_next_waypoint()` reassembles `gps_nav_id`; distance comes straight from `gps_dme_dist_m` (documented in nautical miles despite the `_m`). Both reflect the **active leg only** — X-Plane exposes no dataref for the flight plan's final destination; that requires walking the FMS entries via the plugin SDK, out of scope for a UDP-only script.

### 5.6 FSEconomy status and sim-pause write

Two features don't fit the read-and-mirror pattern:

- **FSEconomy button** — checked *every* loop cycle regardless of whether any dataref changed, since the flash state depends on wall-clock time, not data.
- **Auto-pause (F2)** — the one place this script **writes** to X-Plane instead of only reading. `XPlaneNetworkThread.write_dataref()` sends a `DREF` UDP packet (509 bytes: 5-byte header + 4-byte float + path padded to 500 bytes) to set `sim/time/sim_speed = 0`. It's edge-triggered — fires once when Distance to WPT drops to `AUTO_PAUSE_DISTANCE_NM` (5.0 nm) or below, then re-arms only once distance climbs back above threshold — so a manual unpause afterward, while still close, isn't immediately stomped back to 0.

### 5.7 The main loop: change detection, F1/F2, and reconnection

Each ~100 ms cycle:

1. Check `net_thread.last_data_received` against `CONNECTION_TIMEOUT_SEC`. If disconnected, show the awaiting screen (once per transition) and skip to the next iteration.
2. On the cycle a connection is (re)detected, treat it exactly like F1: push every value to MATRIC unconditionally, not a diff against stale pre-drop state.
3. Snapshot `shared_states` under the lock; poll `poll_function_keys()` — a non-blocking `msvcrt`-based check that drains the whole input buffer each call, returning `(f1, f2)`.
4. FSEconomy and auto-pause are checked unconditionally (see above).
5. If nothing changed and neither hotkey fired, do nothing further — this is what keeps CPU and MATRIC traffic near-zero at idle.
6. Otherwise redraw once, then push only what changed — each control compared against its own `last_*` tracking variable.

F1 and reconnection reuse the same "push everything" code path (`refresh_requested`) rather than duplicating full-push logic.

---

## Appendix A — full dataref reference

| ID | Dataref | Notes |
|---|---|---|
| 1 | `sim/cockpit/electrical/battery_on` | Boolean |
| 2 | `sim/cockpit2/electrical/generator_on[0]` | Boolean, debounced (5 readings) |
| 3 | `sim/cockpit/electrical/avionics_on` | Boolean |
| 4 | `sim/cockpit2/electrical/cross_tie` | Boolean |
| 5 | `sim/cockpit2/engine/actuators/fuel_pump_on[0]` | Boolean, debounced (5 readings) |
| 6 | `sim/cockpit2/switches/beacon_on` | Boolean |
| 7 | `sim/cockpit2/switches/landing_lights_on` | Boolean |
| 8 | `sim/cockpit2/switches/taxi_light_on` | Boolean |
| 9 | `sim/cockpit2/switches/navigation_lights_on` | Boolean |
| 10 | `sim/cockpit2/switches/strobe_lights_on` | Boolean |
| 11 | `sim/cockpit2/ice/ice_pitot_heat_on_pilot` | Boolean |
| 12 | `sim/cockpit2/controls/elevator_trim` | Ratio −1.0..1.0 |
| 13 | `sim/cockpit2/controls/flap_handle_deploy_ratio` | Ratio 0.0..1.0 |
| 14 | `sim/cockpit2/controls/wheel_brake_ratio` | Ratio 0.0..1.0 |
| 15 | `sim/cockpit2/engine/actuators/ignition_key[0]` | Discrete 0–4 (OFF/RIGHT/LEFT/BOTH/START) |
| 16 | `sim/cockpit2/engine/actuators/mixture_ratio[0]` | Ratio 0.0..1.0 |
| 17 | `sim/cockpit2/engine/actuators/throttle_ratio[0]` | Ratio 0.0..1.0 |
| 18 | `sim/cockpit2/tcas/targets/position/yoke_yaw[0]` | Raw value, display only |
| 19 | `sim/cockpit2/autopilot/servos_on` | Boolean |
| 20 | `sim/flightmodel/controls/parkbrake` | Boolean (thresholded) |
| 21 | `sim/cockpit2/fuel/fuel_quantity[0]` | Left tank, kg |
| 22 | `sim/cockpit2/fuel/fuel_quantity[1]` | Right tank, kg |
| 23 | `sim/cockpit2/annunciators/low_vacuum` | Boolean (alarm) |
| 24 | `sim/cockpit2/annunciators/low_voltage` | Boolean (alarm) |
| 25 | `sim/cockpit2/annunciators/oil_pressure` | Boolean (alarm) |
| 26 | `sim/cockpit/switches/HSI_selector` | Discrete: 0=NAV, 2=GPS |
| 28–67 | `sim/aircraft/view/acf_tailnum[0..39]` | 40 chars, ASCII code per index |
| 68 | `fse/status/connected` | Boolean |
| 69 | `fse/status/flying` | >0 means flying |
| 70–77 | `sim/cockpit2/radios/indicators/gps_nav_id[0..7]` | 8 chars, ASCII code per index (active leg) |
| 78 | `sim/cockpit/radios/gps_dme_dist_m` | Nautical miles (despite the name), active leg |
| 79 | `sim/time/sim_speed` | **Writable** — driven to 0 by the F2 auto-pause feature |

---

## Appendix B — MATRIC control reference

### On/off buttons (`SETBUTTONSVISUALSTATE`)

| MATRIC button name | Driven by |
|---|---|
| `BATTERY` | Dataref 1 |
| `ALTERNATOR` | Dataref 2 |
| `AVIONICS` | Dataref 3 |
| `CROSSTIE` | Dataref 4 |
| `FUELPUMP` | Dataref 5 |
| `BEACON` | Dataref 6 |
| `LANDING_LIGHT` | Dataref 7 |
| `TAXI_LIGHT` | Dataref 8 |
| `NAV_LIGHT` | Dataref 9 |
| `STROBE_LIGHT` | Dataref 10 |
| `PITOT_HEAT` | Dataref 11 |
| `AP_SERVO` | Dataref 19 |
| `PARK_BRAKE` | Dataref 20 |
| `VAC_WARNING` | Dataref 23 (low_vacuum) |
| `VOLTS_WARNING` | Dataref 24 (low_voltage) |
| `OIL_WARNING` | Dataref 25 (oil_pressure) |

### Flap position group (mutually exclusive, `SETBUTTONSVISUALSTATE`)

| MATRIC button name | Fires when flap ratio (dataref 13) is closest to |
|---|---|
| `FLAPS_UP` | 0.0 |
| `FLAPS_10` | 0.3333 |
| `FLAPS_20` | 0.666 |
| `FLAPS_FULL` | 1.0 |

### Sliders, 0–100 (`SETCONTROLSSTATE` / value)

| MATRIC slider name | Driven by | Mapping |
|---|---|---|
| `ELEV_TRIM` | Dataref 12 (−1.0..1.0) | Inverted: −1.0→100, 0→50, +1.0→0 |
| `MIXTURE` | Dataref 16 (0.0..1.0) | Direct: ×100 |
| `THROTTLE` | Dataref 17 (0.0..1.0) | Direct: ×100 |

### Multi-position switch (`SETCONTROLSSTATE` / position)

| MATRIC switch name | Position | Meaning |
|---|---|---|
| `MAGNETOS_STARTER` | 0 | OFF |
| `MAGNETOS_STARTER` | 1 | RIGHT |
| `MAGNETOS_STARTER` | 2 | LEFT |
| `MAGNETOS_STARTER` | 3 | BOTH |
| `MAGNETOS_STARTER` | 4 | START |

Position numbering is set by `IGNITION_KEY_POSITION_BASE` (currently **0**) — MATRIC has changed this convention between versions; if the switch lands one position off on a different install, change that constant to 1.

### Text / composite buttons (`SETBUTTONPROPSEX` + `SETBUTTONSVISUALSTATE`)

| MATRIC button name | Behavior |
|---|---|
| `FUEL_WARNING` | Activated (lit) when either tank's `fuel_quantity` (21/22) drops below 10.0 kg. Text switches between `"L FUEL R"` (neither/both low), `"L FUEL   "` (left only), and `"   FUEL R"` (right only). |
| `CALLSIGN` | Text-only, always mirrors the tail number (28–67). No on/off state sent. |
| `FSEconomy` | Solid on when `connected`=1 and `flying`>0. Flashes at 1 Hz when `connected`=1 and `flying`=0. Off when `connected`=0. |

### Not wired to MATRIC (console readouts only)

`Next Waypoint` (70–77) and `Distance to WPT` (78) are shown on the dashboard but not currently pushed to any MATRIC control.

---

*End of document — v0.77. Update alongside the script as more datarefs and MATRIC controls are added.*
