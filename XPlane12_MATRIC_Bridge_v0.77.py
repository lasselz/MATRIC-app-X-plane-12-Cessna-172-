# X-Plane 12 -> MATRIC Cockpit Bridge
# Version: 0.77

import json
import socket
import struct
import sys
import time
import os
import threading

try:
    import msvcrt  # Windows only — used for non-blocking F1 detection
except ImportError:
    msvcrt = None

# --- CONFIGURATION ---
XP_IP = "127.0.0.1"
XP_PORT = 49000
LOCAL_PORT = 49003

DATAREFS = {
    1: b"sim/cockpit/electrical/battery_on",
    2: b"sim/cockpit2/electrical/generator_on[0]",
    3: b"sim/cockpit/electrical/avionics_on",
    4: b"sim/cockpit2/electrical/cross_tie",
    5: b"sim/cockpit2/engine/actuators/fuel_pump_on[0]",
    6: b"sim/cockpit2/switches/beacon_on",
    7: b"sim/cockpit2/switches/landing_lights_on",
    8: b"sim/cockpit2/switches/taxi_light_on",
    9: b"sim/cockpit2/switches/navigation_lights_on",
    10: b"sim/cockpit2/switches/strobe_lights_on",
    11: b"sim/cockpit2/ice/ice_pitot_heat_on_pilot",
    12: b"sim/cockpit2/controls/elevator_trim",
    13: b"sim/cockpit2/controls/flap_handle_deploy_ratio",
    14: b"sim/cockpit2/controls/wheel_brake_ratio",
    # Per-engine array datarefs — indexed [0] for engine 1, same convention
    # as generator_on[0] / fuel_pump_on[0] above. Change the index if you
    # need a different engine.
    15: b"sim/cockpit2/engine/actuators/ignition_key[0]",
    16: b"sim/cockpit2/engine/actuators/mixture_ratio[0]",
    17: b"sim/cockpit2/engine/actuators/throttle_ratio[0]",
    # Per-TCAS-target array — indexed [0]. Double check this is the dataref
    # you meant; it reflects a TCAS target's yoke position, not necessarily
    # your own aircraft's yoke input.
    18: b"sim/cockpit2/tcas/targets/position/yoke_yaw[0]",
    19: b"sim/cockpit2/autopilot/servos_on",
    20: b"sim/flightmodel/controls/parkbrake",
    # Per-tank array — indexed for the 2 tanks.
    # Per-tank fuel quantity in kg (not the sim's own annunciator — that
    # one behaves oddly per observation, see fuel low-quantity handling
    # below for the 10.1 kg threshold this drives instead).
    21: b"sim/cockpit2/fuel/fuel_quantity[0]",
    22: b"sim/cockpit2/fuel/fuel_quantity[1]",
    23: b"sim/cockpit2/annunciators/low_vacuum",
    24: b"sim/cockpit2/annunciators/low_voltage",
    25: b"sim/cockpit2/annunciators/oil_pressure",
    # Selector, not boolean: 0 = NAV, 2 = GPS.
    26: b"sim/cockpit/switches/HSI_selector",
    # FSEconomy plugin status — drives the flashing/solid FSEconomy button.
    68: b"fse/status/connected",
    69: b"fse/status/flying",
}

# acf_tailnum is a string dataref — X-Plane's UDP interface can only send
# floats, so a string has to be subscribed one character at a time (each
# index returns that character's ASCII code as a float) and reassembled.
# It's 40 bytes long, hence ids 28-67.
TAILNUM_LENGTH = 40
TAILNUM_FIRST_ID = 28
DATAREFS.update({
    TAILNUM_FIRST_ID + i: f"sim/aircraft/view/acf_tailnum[{i}]".encode("ascii")
    for i in range(TAILNUM_LENGTH)
})
TAILNUM_DATAREF_IDS = set(range(TAILNUM_FIRST_ID, TAILNUM_FIRST_ID + TAILNUM_LENGTH))

# GPS/FMS active-leg waypoint ID — same byte[N] flat-string family as
# acf_tailnum, so subscribed the same way (one character per id) and
# reassembled. This is the CURRENT leg's target, not the flight plan's
# final destination (X-Plane exposes no dataref for that — it requires
# walking the FMS entries via the plugin SDK).
GPS_NAV_ID_LENGTH = 8  # generous headroom; real IDs are usually <=5 chars
GPS_NAV_ID_FIRST_ID = 70
DATAREFS.update({
    GPS_NAV_ID_FIRST_ID + i: f"sim/cockpit2/radios/indicators/gps_nav_id[{i}]".encode("ascii")
    for i in range(GPS_NAV_ID_LENGTH)
})
GPS_NAV_ID_DATAREF_IDS = set(range(GPS_NAV_ID_FIRST_ID, GPS_NAV_ID_FIRST_ID + GPS_NAV_ID_LENGTH))

# Distance to that same active-leg waypoint. Despite the "_m" in the
# name this is documented in nautical miles, not meters.
GPS_DME_DIST_ID = 78
DATAREFS[GPS_DME_DIST_ID] = b"sim/cockpit/radios/gps_dme_dist_m"
GPS_DME_DIST_DATAREF_IDS = {GPS_DME_DIST_ID}

# Simulation rate — read for display, and written (to 0, i.e. paused) by
# the F2 auto-pause feature below. Writable, unlike everything else in
# DATAREFS so far.
SIM_SPEED_ID = 79
DATAREFS[SIM_SPEED_ID] = b"sim/time/sim_speed"

# F2 toggles this feature on/off (starts OFF). While on, the sim is
# paused (sim_speed set to 0) the moment Distance to WPT drops to this
# threshold or below — once per approach, not repeatedly, so a manual
# unpause afterward isn't immediately stomped back to 0.
AUTO_PAUSE_DISTANCE_NM = 5.0

# Datarefs whose value is a continuous ratio/position rather than a simple
# on/off switch. Used to pick a display formatter below.
RATIO_DATAREFS = {12, 13, 14, 16, 17}
IGNITION_KEY_ID = 15
YOKE_YAW_ID = 18

# Per-tank fuel quantity in kg (ids 21/22) — continuous, not boolean, and
# compared against this threshold to drive the ALARM/NORMAL indication
# instead of relying on the sim's own fuel_transfer/fuel_quantity
# annunciators (observed to behave oddly).
FUEL_QTY_DATAREF_IDS = {21, 22}
FUEL_LOW_THRESHOLD_KG = 10.0

# FUEL_WARNING is a single MATRIC button covering both tanks, with its text
# indicating which tank (if any) is the cause. Not a 1:1 dataref->button
# mapping like MATRIC_BUTTONS, so it's handled separately below.
FUEL_WARNING_BUTTON = "FUEL_WARNING"
FUEL_WARNING_TEXT_NONE = "L FUEL R"
FUEL_WARNING_TEXT_LEFT = "L FUEL   "
FUEL_WARNING_TEXT_RIGHT = "   FUEL R"
FUEL_WARNING_TEXT_BOTH = "L FUEL R"  # not specified — defaults to the neutral text

# Tail number (reassembled from ids 28-67) mirrored as this button's text.
CALLSIGN_BUTTON = "CALLSIGN"


def fuel_warning_state(snap):
    """Returns (activated, text) for the FUEL_WARNING button."""
    left_alarm = snap.get(21, 0.0) < FUEL_LOW_THRESHOLD_KG
    right_alarm = snap.get(22, 0.0) < FUEL_LOW_THRESHOLD_KG
    activated = left_alarm or right_alarm
    if left_alarm and right_alarm:
        text = FUEL_WARNING_TEXT_BOTH
    elif left_alarm:
        text = FUEL_WARNING_TEXT_LEFT
    elif right_alarm:
        text = FUEL_WARNING_TEXT_RIGHT
    else:
        text = FUEL_WARNING_TEXT_NONE
    return activated, text

# FSEconomy connection/flight status drives a single button:
#   connected=1, flying=0    -> flash at 1 Hz
#   connected=1, flying>0    -> solid on
#   connected=0 (any flying) -> off
# Not a 1:1 boolean mapping (the flash needs wall-clock time, not just a
# dataref value), so it's handled separately like FUEL_WARNING/CALLSIGN.
FSE_CONNECTED_ID = 68
FSE_FLYING_ID = 69
FSE_BUTTON = "FSEconomy"
FSE_FLASH_PERIOD_SEC = 1.0  # full on/off cycle length for the 1 Hz flash


def fse_button_desired_state(snap, now):
    """Returns the button's desired on/off bool for this instant. For the
    flashing case this depends on wall-clock time, so it must be
    re-evaluated every loop cycle, not just when a dataref value changes."""
    connected = snap.get(FSE_CONNECTED_ID, 0.0) > 0.5
    flying = snap.get(FSE_FLYING_ID, 0.0) > 0.0
    if connected and flying:
        return True
    if connected and not flying:
        # Toggle every half-period: on for the first half of each cycle,
        # off for the second.
        return (now % FSE_FLASH_PERIOD_SEC) < (FSE_FLASH_PERIOD_SEC / 2.0)
    return False  # not connected

IGNITION_KEY_LABELS = {0: "OFF", 1: "RIGHT", 2: "LEFT", 3: "BOTH", 4: "START"}

HSI_SELECTOR_ID = 26
HSI_SELECTOR_LABELS = {0: "NAV", 2: "GPS"}

# Ignition key (dataref 15, values 0-4) pushed to a MATRIC "5 Position
# switch" instead of a button. MATRIC's documented position numbering
# starts at 1, but a later MATRIC version changed multi-position switches
# to start at 0 — flip this to 0 if the switch lands one position off.
IGNITION_KEY_SWITCH = "MAGNETOS_STARTER"
IGNITION_KEY_POSITION_BASE = 0


def ignition_key_switch_position(value):
    idx = max(0, min(4, int(round(value))))  # 0=OFF .. 4=START
    return idx + IGNITION_KEY_POSITION_BASE

# Precomputed once — the subscription packets never change, so build them
# a single time instead of re-packing every reconnect.
_SUB_HEADER = b"RREF\x00"
_SUB_FREQ = struct.pack("<I", 30)
SUBSCRIBE_PACKETS = [
    _SUB_HEADER + _SUB_FREQ + struct.pack("<I", dref_id) + dref_str.ljust(400, b"\x00")
    for dref_id, dref_str in DATAREFS.items()
]

# --- STATE STORAGE ---
# Built from DATAREFS' own keys rather than a hardcoded range, so adding or
# removing a dataref above can't silently fall out of sync with storage.
raw_states = {i: 0.0 for i in DATAREFS}
shared_states = {i: 0.0 for i in DATAREFS}
state_lock = threading.Lock()

# --- FILTER SETTINGS ---
FILTERED_DATAREFS = {2, 5}
REQUIRED_READINGS = 5

pending_states = {2: None, 5: None}
pending_counts = {2: 0, 5: 0}

# --- MATRIC INTEGRATION ---
# https://github.com/tgudelj/MATRICIntegrationDemo
# The MATRIC PC app always listens for the Integration API on UDP
# 127.0.0.1:50300. Commands are plain JSON. Requires:
#   1. Integration API enabled in MATRIC: edit
#      %USERPROFILE%\Documents\.matric\config.json and set
#      "EnableIntegrationAPI": true, then restart MATRIC.
#   2. A deck loaded on the tablet with buttons/controls named to match
#      MATRIC_BUTTONS below (rename either side so they agree).
MATRIC_HOST = "127.0.0.1"
MATRIC_PORT = 5300
MATRIC_APP_NAME = "Xplane12Deck"
# MATRIC stores authorized apps (and their PIN) in its own config file —
# there is no PIN shown on screen to type in. CONNECT just triggers an
# "Authorization required" popup that you accept, and MATRIC writes the
# PIN here itself, keyed by appName.
MATRIC_CONFIG_PATH = os.path.join(os.path.expanduser("~"), "Documents", ".matric", "config.json")
# Local cache so you only have to type the PIN once, even via double-click
# runs (where there's no console to type into on later runs anyway).
MATRIC_PIN_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".matric_pin")

# Maps our dataref IDs to the button names as defined in your MATRIC deck.
# Rename these values to match the button names you actually used in the
# MATRIC editor (right-click a control -> properties -> Name).
MATRIC_BUTTONS = {
    1: "BATTERY",
    2: "ALTERNATOR",
    3: "AVIONICS",
    4: "CROSSTIE",
    5: "FUELPUMP",
    6: "BEACON",
    7: "LANDING_LIGHT",
    8: "TAXI_LIGHT",
    9: "NAV_LIGHT",
    10: "STROBE_LIGHT",
    11: "PITOT_HEAT",
    19: "AP_SERVO",
    20: "PARK_BRAKE",
    23: "VAC_WARNING",
    24: "VOLTS_WARNING",
    25: "OIL_WARNING",
}

# Flap handle position (dataref 13, a continuous 0.0-1.0 ratio) mapped to
# one of 4 discrete MATRIC buttons instead of an on/off switch. Whichever
# target ratio the current position is closest to is the "active" one —
# the others are always sent as off, so exactly one is lit at a time.
FLAP_RATIO_DATAREF_ID = 13
FLAP_BUTTONS = {
    "FLAPS_UP": 0.0,
    "FLAPS_10": 0.3333,
    "FLAPS_20": 0.666,
    "FLAPS_FULL": 1.0,
}


def active_flap_button(ratio):
    return min(FLAP_BUTTONS, key=lambda name: abs(ratio - FLAP_BUTTONS[name]))


def flap_button_states(ratio):
    """All 4 flap buttons' on/off state — only the nearest-matching one is on."""
    active = active_flap_button(ratio)
    return {name: (name == active) for name in FLAP_BUTTONS}

# Elevator trim (dataref 12, a -1.0..1.0 ratio) pushed to a MATRIC slider
# instead of a button. Sliders take an integer 0-100, so -1.0/0/+1.0 map
# to 0/50/100.
ELEV_TRIM_DATAREF_ID = 12
ELEV_TRIM_SLIDER = "ELEV_TRIM"


def elev_trim_slider_value(ratio):
    # Inverted: -1.0 -> 100, 0 -> 50, +1.0 -> 0.
    clamped = max(-1.0, min(1.0, ratio))
    return round((1.0 - clamped) / 2.0 * 100)

# Mixture and throttle (datarefs 16/17, 0.0-1.0 ratios) pushed to their own
# MATRIC sliders — direct 0-100 mapping, no inversion.
MIXTURE_DATAREF_ID = 16
MIXTURE_SLIDER = "MIXTURE"
THROTTLE_DATAREF_ID = 17
THROTTLE_SLIDER = "THROTTLE"


def unit_ratio_slider_value(ratio):
    clamped = max(0.0, min(1.0, ratio))
    return round(clamped * 100)


class MatricClient:
    """Minimal UDP client for the MATRIC Integration API.

    Pairing: CONNECT triggers an "Authorization Required" popup on the PC
    that shows a PIN. Type that PIN in here once — it's cached locally so
    you won't be asked again, even on later double-click runs where there's
    no console to type into.
    """

    def __init__(self, app_name=MATRIC_APP_NAME, host=MATRIC_HOST, port=MATRIC_PORT):
        self.app_name = app_name
        self.host = host
        self.port = port
        self.pin = None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(2.0)

    def _send(self, payload: dict):
        self.sock.sendto(json.dumps(payload).encode("utf-8"), (self.host, self.port))

    def _connect(self):
        """Triggers the authorization popup on the MATRIC PC app."""
        self._send({"command": "CONNECT", "appName": self.app_name})

    def _read_matric_config(self):
        try:
            with open(MATRIC_CONFIG_PATH, "r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def _pin_from_config(self):
        config = self._read_matric_config()
        if not config:
            return None
        for entry in config.get("AuthorizedApps", []):
            if entry.get("appName") == self.app_name:
                pin = entry.get("appPin")
                return pin.strip() if pin else None
        return None

    def _load_cached_pin(self):
        if os.path.exists(MATRIC_PIN_CACHE_FILE):
            with open(MATRIC_PIN_CACHE_FILE, "r") as f:
                pin = f.read().strip()
                return pin or None
        return None

    def _save_cached_pin(self, pin: str):
        with open(MATRIC_PIN_CACHE_FILE, "w") as f:
            f.write(pin)

    def ensure_paired(self):
        """Uses an already-authorized PIN (from MATRIC's own config, or a
        local cache from a previous run), or walks through pairing."""
        pin = self._pin_from_config() or self._load_cached_pin()
        if pin:
            self.pin = pin
            print("MATRIC: using existing authorization.")
            return

        print(f"MATRIC: no authorization found for '{self.app_name}' yet, requesting one.")
        try:
            self._connect()
        except OSError as e:
            print(f"MATRIC: could not reach the app ({e}) — integration disabled this run.")
            return

        print("MATRIC: check the PC for an 'Authorization Required' popup — it shows a PIN.")
        try:
            pin = input("MATRIC: type that PIN here and press Enter (blank to skip): ").strip()
        except (EOFError, OSError):
            print("MATRIC: no interactive console available to type a PIN into. "
                  "Run this script from a terminal once (python "
                  f"{os.path.basename(__file__)}) to pair — after that the PIN "
                  "is cached and double-clicking will work fine.")
            return

        if not pin:
            print("MATRIC: skipped — integration disabled this run.")
            return

        self.pin = pin
        self._save_cached_pin(pin)
        print("MATRIC: PIN saved — integration enabled, and cached for next time.")

    def set_button_states(self, states: dict):
        """states: {button_name: True/False}. Broadcasts to all connected clients."""
        if not self.pin or not states:
            return
        data = [{"buttonName": name, "state": "on" if on else "off"} for name, on in states.items()]
        try:
            self._send({
                "command": "SETBUTTONSVISUALSTATE",
                "appName": self.app_name,
                "appPIN": self.pin,
                "clientId": None,  # broadcast to every connected client
                "data": data,
            })
        except OSError:
            pass  # MATRIC app unreachable right now — non-fatal, next change will retry

    def set_slider_values(self, values: dict):
        """values: {control_name: int 0-100}. Broadcasts to all connected clients."""
        if not self.pin or not values:
            return
        data = [{"controlName": name, "state": {"value": val}} for name, val in values.items()]
        try:
            self._send({
                "command": "SETCONTROLSSTATE",
                "appName": self.app_name,
                "appPIN": self.pin,
                "clientId": None,  # broadcast to every connected client
                "data": data,
            })
        except OSError:
            pass  # MATRIC app unreachable right now — non-fatal, next change will retry

    def set_switch_positions(self, positions: dict):
        """positions: {control_name: int position}. Broadcasts to all connected clients."""
        if not self.pin or not positions:
            return
        data = [{"controlName": name, "state": {"position": pos}} for name, pos in positions.items()]
        try:
            self._send({
                "command": "SETCONTROLSSTATE",
                "appName": self.app_name,
                "appPIN": self.pin,
                "clientId": None,  # broadcast to every connected client
                "data": data,
            })
        except OSError:
            pass  # MATRIC app unreachable right now — non-fatal, next change will retry

    def set_button_texts(self, texts: dict):
        """texts: {button_name: str}. Broadcasts to all connected clients.
        Uses SETBUTTONPROPSEX — a visual-only property, separate from the
        on/off state set by set_button_states."""
        if not self.pin or not texts:
            return
        data = [{"buttonName": name, "text": text} for name, text in texts.items()]
        try:
            self._send({
                "command": "SETBUTTONPROPSEX",
                "appName": self.app_name,
                "appPIN": self.pin,
                "clientId": None,  # broadcast to every connected client
                "data": data,
            })
        except OSError:
            pass  # MATRIC app unreachable right now — non-fatal, next change will retry


def rounded_for_comparison(snap):
    """Rounds continuous values to display precision before comparing
    snapshots, so tiny float jitter (e.g. trim wobbling by 0.0001) doesn't
    trigger a redraw when the printed value wouldn't actually change."""
    out = dict(snap)
    for dref_id in RATIO_DATAREFS:
        out[dref_id] = round(snap[dref_id], 3)
    out[YOKE_YAW_ID] = round(snap[YOKE_YAW_ID], 3)
    for dref_id in FUEL_QTY_DATAREF_IDS:
        out[dref_id] = round(snap[dref_id], 1)  # display precision is 1 decimal kg
    out[GPS_DME_DIST_ID] = round(snap[GPS_DME_DIST_ID], 1)  # display precision is 1 decimal nm
    return out


def diff_button_states(old_snap, new_snap):
    """Returns {button_name: bool} only for datarefs whose ON/OFF state changed."""
    changed = {}
    for dref_id, name in MATRIC_BUTTONS.items():
        new_on = new_snap.get(dref_id, 0.0) > 0.5
        if old_snap is None or (old_snap.get(dref_id, 0.0) > 0.5) != new_on:
            changed[name] = new_on
    return changed


def all_button_states(snap):
    """Every MATRIC_BUTTONS entry's current ON/OFF, regardless of whether it
    changed — used for a manual full resync (F1) instead of the normal
    diff-only push, in case MATRIC and the sim ever drift out of sync."""
    states = {name: snap.get(dref_id, 0.0) > 0.5 for dref_id, name in MATRIC_BUTTONS.items()}
    states.update(flap_button_states(snap.get(FLAP_RATIO_DATAREF_ID, 0.0)))
    return states


def poll_function_keys():
    """Non-blocking check for F1/F2 (Windows console only, via msvcrt).
    Drains the whole keyboard buffer each call so unrelated keypresses
    don't pile up; returns (f1_pressed, f2_pressed)."""
    f1 = f2 = False
    if msvcrt is None:
        return f1, f2
    while msvcrt.kbhit():
        first = msvcrt.getch()
        if first in (b"\x00", b"\xe0"):  # function/arrow key prefix
            second = msvcrt.getch()
            if second == b";":  # F1 scancode
                f1 = True
            elif second == b"<":  # F2 scancode
                f2 = True
    return f1, f2


class XPlaneNetworkThread(threading.Thread):
    def __init__(self):
        super().__init__()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", LOCAL_PORT))
        self.sock.settimeout(1.0)
        self.running = True
        # Two separate clocks: last_data_received is the true "are we
        # actually hearing from X-Plane" signal the main loop uses to show
        # the awaiting-connection screen; last_subscribe_attempt just
        # throttles how often we resend subscriptions while waiting, so it
        # can't be used to answer "are we connected".
        self.last_data_received = time.time()
        self.last_subscribe_attempt = time.time()

    def subscribe_all(self):
        print("Sending subscriptions to X-Plane 12...")
        for packet in SUBSCRIBE_PACKETS:
            try:
                self.sock.sendto(packet, (XP_IP, XP_PORT))
            except OSError as e:
                print(f"Subscription send failed: {e}")
                break

    def write_dataref(self, dataref_path: bytes, value: float):
        """Writes a dataref via X-Plane's UDP DREF command. Packet is 509
        bytes total: 5-byte "DREF\\0" header + 4-byte float + the path,
        null-padded out to fill the remaining 500 bytes."""
        header = b"DREF\x00"
        val_bytes = struct.pack("<f", value)
        path_padded = dataref_path.ljust(500, b"\x00")
        packet = header + val_bytes + path_padded
        try:
            self.sock.sendto(packet, (XP_IP, XP_PORT))
        except OSError as e:
            print(f"DREF write failed: {e}")

    def _maybe_resubscribe(self):
        now = time.time()
        if now - self.last_subscribe_attempt > 3.0:
            self.subscribe_all()
            self.last_subscribe_attempt = now

    def run(self):
        self.subscribe_all()

        while self.running:
            try:
                data, addr = self.sock.recvfrom(65535)
                self.last_data_received = time.time()

                if data[0:4] == b"RREF":
                    # Parse the whole packet first, without holding the lock.
                    # X-Plane batches every subscribed dataref into one packet
                    # at 30Hz, so this avoids acquiring the lock up to 11
                    # times per packet.
                    updates = {}
                    offset = 5
                    packet_len = len(data)
                    while offset <= packet_len - 8:
                        dref_id, value = struct.unpack("<If", data[offset:offset + 8])
                        if dref_id in raw_states:
                            updates[dref_id] = value
                        offset += 8

                    if updates:
                        with state_lock:
                            for dref_id, value in updates.items():
                                raw_states[dref_id] = value

                                if dref_id in FILTERED_DATAREFS:
                                    new_state = 1.0 if value > 0.5 else 0.0
                                    current_state = shared_states[dref_id]

                                    if new_state == current_state:
                                        pending_states[dref_id] = None
                                        pending_counts[dref_id] = 0
                                    else:
                                        if pending_states[dref_id] != new_state:
                                            pending_states[dref_id] = new_state
                                            pending_counts[dref_id] = 1
                                        else:
                                            pending_counts[dref_id] += 1

                                            if pending_counts[dref_id] >= REQUIRED_READINGS:
                                                shared_states[dref_id] = new_state
                                                pending_states[dref_id] = None
                                                pending_counts[dref_id] = 0
                                elif dref_id in RATIO_DATAREFS or dref_id in FUEL_QTY_DATAREF_IDS or dref_id in TAILNUM_DATAREF_IDS or dref_id in GPS_NAV_ID_DATAREF_IDS or dref_id in GPS_DME_DIST_DATAREF_IDS or dref_id in (IGNITION_KEY_ID, YOKE_YAW_ID, HSI_SELECTOR_ID):
                                    # Continuous/discrete values — stored as-is, not boolean.
                                    shared_states[dref_id] = value
                                else:
                                    shared_states[dref_id] = value

            except socket.timeout:
                self._maybe_resubscribe()
            except OSError:
                # On Windows, a previous sendto() to a port nobody's
                # listening on (X-Plane closed) can come back as an ICMP
                # "port unreachable", which the OS then raises as a
                # ConnectionResetError/OSError on the *next* recvfrom() —
                # not on the send itself. Without catching this, the
                # thread would die silently the moment X-Plane closes.
                # Treated exactly like a timeout: keep retrying, don't crash.
                self._maybe_resubscribe()

    def stop(self):
        self.running = False
        self.sock.close()


def fmt(val):
    return "\033[92mON\033[0m" if val > 0.5 else "\033[91mOFF\033[0m"


def fmt_annunciator(val):
    """Warning-light style: lit is bad (red ALARM), unlit is good (green NORMAL)."""
    return "\033[91mALARM\033[0m" if val > 0.5 else "\033[92mNORMAL\033[0m"


def fmt_fuel_qty(kg):
    """Shows the raw quantity plus an ALARM/NORMAL verdict against the
    low-fuel threshold, since this isn't a boolean annunciator."""
    status = "\033[91mALARM\033[0m" if kg < FUEL_LOW_THRESHOLD_KG else "\033[92mNORMAL\033[0m"
    return f"{kg:6.1f} kg  {status}"


def read_tail_number(snap):
    """Reassembles acf_tailnum from its per-character subscriptions,
    stopping at the first null/non-positive byte (string terminator)."""
    chars = []
    for i in range(TAILNUM_LENGTH):
        code = int(round(snap.get(TAILNUM_FIRST_ID + i, 0.0)))
        if code <= 0:
            break
        chars.append(chr(code))
    return "".join(chars).strip() or "N/A"


def read_next_waypoint(snap):
    """Same reassembly as read_tail_number, for gps_nav_id (active-leg
    waypoint identifier)."""
    chars = []
    for i in range(GPS_NAV_ID_LENGTH):
        code = int(round(snap.get(GPS_NAV_ID_FIRST_ID + i, 0.0)))
        if code <= 0:
            break
        chars.append(chr(code))
    return "".join(chars).strip() or "N/A"


def fmt_gps_dist(nm):
    return f"{nm:6.1f} nm"


def fmt_signed_pct(val):
    """For -1.0..1.0 ratios (e.g. elevator trim): signed percentage."""
    return f"{val * 100:+6.1f}%"


def fmt_pct(val):
    """For 0.0..1.0 ratios (flaps, park brake, mixture, throttle)."""
    return f"{val * 100:5.1f}%"


def fmt_ignition(val):
    idx = int(round(val))
    return IGNITION_KEY_LABELS.get(idx, f"UNK({idx})")


def fmt_hsi_selector(val):
    idx = int(round(val))
    return HSI_SELECTOR_LABELS.get(idx, f"UNK({idx})")


def fmt_raw(val):
    """No assumed range — shown as-is for values like TCAS target yoke yaw."""
    return f"{val:+7.3f}"


def build_dashboard(snap, status_line="", auto_pause_enabled=False):
    # Cursor-home only — the layout never changes shape, so we overwrite
    # in place instead of clearing + repainting the whole screen, which
    # is what causes visible flicker.
    lines = [
        "=========================================",
        "      X-PLANE 12 COCKPIT SYSTEM MONITOR  ",
        f"      Tail Number: {read_tail_number(snap)}",
        "=========================================",
        " 1. ELECTRICAL SYSTEMS:",
        f"  Main Battery (Bat 1) :  {fmt(snap[1])}",
        f"  Alternator 1         :  {fmt(snap[2])}",
        f"  Avionics 1           :  {fmt(snap[3])}",
        f"  Avionics Cross-Tie   :  {fmt(snap[4])}",
        "-----------------------------------------",
        " 2. ENGINE SYSTEMS:",
        f"  Fuel Pump 1          :  {fmt(snap[5])}",
        "-----------------------------------------",
        " 3. AIRCRAFT LIGHTS (SWITCHES):",
        f"  Beacon Light         :  {fmt(snap[6])}",
        f"  Landing Light        :  {fmt(snap[7])}",
        f"  Taxi Light           :  {fmt(snap[8])}",
        f"  Nav Light            :  {fmt(snap[9])}",
        f"  Strobe Light         :  {fmt(snap[10])}",
        "-----------------------------------------",
        " 4. SYSTEMS & ANTI-ICE:",
        f"  Pitot Heat           :  {fmt(snap[11])}",
        f"  Autopilot Servos     :  {fmt(snap[19])}",
        f"  Park Brake           :  {fmt(snap[20])}",
        "-----------------------------------------",
        " 5. FLIGHT CONTROLS & ENGINE:",
        f"  Elevator Trim        :  {fmt_signed_pct(snap[12])}",
        f"  Flap Handle          :  {fmt_pct(snap[13])}",
        f"  Wheel Brake          :  {fmt_pct(snap[14])}",
        f"  Ignition Key (Eng 1) :  {fmt_ignition(snap[15])}",
        f"  Mixture (Eng 1)      :  {fmt_pct(snap[16])}",
        f"  Throttle (Eng 1)     :  {fmt_pct(snap[17])}",
        f"  Yoke Yaw (TCAS tgt 0):  {fmt_raw(snap[18])}",
        "-----------------------------------------",
        " 6. ANNUNCIATORS & SELECTORS:",
        f"  Left Fuel Tank       :  {fmt_fuel_qty(snap[21])}",
        f"  Right Fuel Tank      :  {fmt_fuel_qty(snap[22])}",
        f"  Low Vacuum           :  {fmt_annunciator(snap[23])}",
        f"  Low Voltage          :  {fmt_annunciator(snap[24])}",
        f"  Oil Pressure         :  {fmt_annunciator(snap[25])}",
        f"  HSI Selector         :  {fmt_hsi_selector(snap[26])}",
        f"  Next Waypoint        :  {read_next_waypoint(snap)}",
        f"  Distance to WPT      :  {fmt_gps_dist(snap[GPS_DME_DIST_ID])}",
        "-----------------------------------------",
        " 7. FSECONOMY:",
        f"  Connected            :  {fmt(snap[68])}",
        f"  Flying               :  {fmt_raw(snap[69])}",
        "-----------------------------------------",
        " 8. SIMULATOR:",
        f"  Sim Speed            :  {fmt_raw(snap[SIM_SPEED_ID])}",
        f"  Auto-Pause @ {AUTO_PAUSE_DISTANCE_NM:.1f}nm (F2) :  {'ON' if auto_pause_enabled else 'OFF'}",
        "=========================================",
        " Press Ctrl+C to exit  |  F1: force refresh to MATRIC  |  F2: toggle auto-pause",
        f" {status_line}" if status_line else "",
        "",  # trailing blank line to overwrite any leftover text below
    ]
    # "\033[K" erases from the cursor to the end of each line before the
    # newline, so a shorter value (e.g. "ON") fully overwrites a longer
    # one that was there before (e.g. "OFF") instead of leaving stray
    # characters behind.
    return "\033[H" + "\033[K\n".join(lines) + "\033[K"


CONNECTION_TIMEOUT_SEC = 3.0  # no data from X-Plane for this long -> "disconnected"


def build_awaiting_screen():
    lines = [
        "=========================================",
        "      X-PLANE 12 COCKPIT SYSTEM MONITOR  ",
        "=========================================",
        "",
        "   Awaiting connection to X-Plane 12...",
        "",
        "   This will reconnect automatically once",
        "   X-Plane is running — no need to restart",
        "   this script.",
        "",
        "=========================================",
        " Press Ctrl+C to exit",
        "",
    ]
    return "\033[H" + "\033[K\n".join(lines) + "\033[K"


if __name__ == "__main__":
    # Windows fix: Enables processing of ANSI color codes in standard cmd prompt
    if os.name == 'nt':
        os.system('')

    matric = MatricClient()
    matric.ensure_paired()

    # Clear the screen once up front, then only overwrite from here on.
    sys.stdout.write("\033[2J")

    net_thread = XPlaneNetworkThread()
    net_thread.daemon = True
    net_thread.start()

    if msvcrt is None and os.name != 'nt':
        print("F1 refresh is only available on Windows (msvcrt) — skipping.")

    last_snap = None
    last_flap_button = None
    last_trim_value = None
    last_mixture_value = None
    last_throttle_value = None
    last_ignition_position = None
    last_fuel_warning = None
    last_callsign_text = None
    last_fse_button_state = None
    status_line = ""
    was_connected = False  # forces a first-connect full resync, same as a reconnect
    auto_pause_enabled = False  # starts OFF, toggled by F2
    auto_pause_triggered = False  # edge-trigger latch, see below
    try:
        while True:
            connected = (time.time() - net_thread.last_data_received) < CONNECTION_TIMEOUT_SEC

            if not connected:
                if was_connected:
                    sys.stdout.write(build_awaiting_screen())
                    sys.stdout.flush()
                was_connected = False
                time.sleep(0.2)
                continue

            just_reconnected = not was_connected
            was_connected = True

            with state_lock:
                snap = shared_states.copy()

            # Compare at display precision, not raw floats — otherwise
            # sub-thousandth jitter on continuous values (trim, throttle,
            # etc.) would force a redraw every cycle even though nothing
            # visibly changed.
            display_snap = rounded_for_comparison(snap)

            # FSEconomy's flash mode depends on wall-clock time, not just a
            # dataref value, so this has to be checked every cycle — it
            # can't wait for "did anything change" like the rest below.
            fse_desired_on = fse_button_desired_state(snap, time.time())
            if fse_desired_on != last_fse_button_state:
                matric.set_button_states({FSE_BUTTON: fse_desired_on})
                last_fse_button_state = fse_desired_on

            f1_pressed, f2_pressed = poll_function_keys()
            status_updated_this_cycle = False

            if f2_pressed:
                auto_pause_enabled = not auto_pause_enabled
                auto_pause_triggered = False  # don't carry a stale latch across a toggle
                state_word = "ENABLED" if auto_pause_enabled else "DISABLED"
                status_line = f"Auto-pause {state_word} at {time.strftime('%H:%M:%S')}"
                status_updated_this_cycle = True

            # Auto-pause: fires once per approach (edge-triggered) rather
            # than every cycle, so a manual unpause afterward — while still
            # inside the threshold — isn't immediately stomped back to 0.
            # The latch resets once distance climbs back above the
            # threshold, so it's ready again for the next waypoint.
            distance_to_wpt = snap.get(GPS_DME_DIST_ID, 0.0)
            if auto_pause_enabled:
                if distance_to_wpt <= AUTO_PAUSE_DISTANCE_NM and not auto_pause_triggered:
                    net_thread.write_dataref(DATAREFS[SIM_SPEED_ID], 0.0)
                    auto_pause_triggered = True
                    status_line = (f"Auto-pause: sim paused at {AUTO_PAUSE_DISTANCE_NM:.1f} nm "
                                    f"from waypoint, {time.strftime('%H:%M:%S')}")
                    status_updated_this_cycle = True
                elif distance_to_wpt > AUTO_PAUSE_DISTANCE_NM:
                    auto_pause_triggered = False

            # A reconnect gets the same full-resync treatment as F1: don't
            # trust the diff against whatever was last known before the
            # drop, just push everything fresh.
            refresh_requested = f1_pressed or just_reconnected
            if refresh_requested:
                matric.set_button_states(all_button_states(snap))
                matric.set_slider_values({
                    ELEV_TRIM_SLIDER: elev_trim_slider_value(snap.get(ELEV_TRIM_DATAREF_ID, 0.0)),
                    MIXTURE_SLIDER: unit_ratio_slider_value(snap.get(MIXTURE_DATAREF_ID, 0.0)),
                    THROTTLE_SLIDER: unit_ratio_slider_value(snap.get(THROTTLE_DATAREF_ID, 0.0)),
                })
                matric.set_switch_positions({
                    IGNITION_KEY_SWITCH: ignition_key_switch_position(snap.get(IGNITION_KEY_ID, 0.0))
                })
                fuel_warning_active, fuel_warning_text = fuel_warning_state(snap)
                matric.set_button_states({FUEL_WARNING_BUTTON: fuel_warning_active})
                matric.set_button_texts({FUEL_WARNING_BUTTON: fuel_warning_text})
                callsign_text = read_tail_number(snap)
                matric.set_button_texts({CALLSIGN_BUTTON: callsign_text})
                last_flap_button = active_flap_button(snap.get(FLAP_RATIO_DATAREF_ID, 0.0))
                last_trim_value = elev_trim_slider_value(snap.get(ELEV_TRIM_DATAREF_ID, 0.0))
                last_mixture_value = unit_ratio_slider_value(snap.get(MIXTURE_DATAREF_ID, 0.0))
                last_throttle_value = unit_ratio_slider_value(snap.get(THROTTLE_DATAREF_ID, 0.0))
                last_ignition_position = ignition_key_switch_position(snap.get(IGNITION_KEY_ID, 0.0))
                last_fuel_warning = (fuel_warning_active, fuel_warning_text)
                last_callsign_text = callsign_text
                if just_reconnected:
                    status_line = f"Reconnected to X-Plane 12 — full resync sent at {time.strftime('%H:%M:%S')}"
                else:
                    status_line = f"Manual refresh sent to MATRIC at {time.strftime('%H:%M:%S')}"

            # Only touch the terminal/MATRIC when something actually
            # changed — avoids a redraw (and its I/O) every 100ms at idle.
            if display_snap != last_snap or refresh_requested or status_updated_this_cycle:
                sys.stdout.write(build_dashboard(snap, status_line, auto_pause_enabled))
                sys.stdout.flush()

                if not refresh_requested:
                    # F1 already pushed the full current state above, so
                    # skip the diff push this cycle to avoid sending twice.
                    changed_buttons = diff_button_states(last_snap, display_snap)

                    current_flap_button = active_flap_button(snap.get(FLAP_RATIO_DATAREF_ID, 0.0))
                    if current_flap_button != last_flap_button:
                        # Push all 4 flap buttons together so the previous
                        # detent turns off exactly when the new one turns on.
                        changed_buttons.update(flap_button_states(snap.get(FLAP_RATIO_DATAREF_ID, 0.0)))
                        last_flap_button = current_flap_button

                    if changed_buttons:
                        matric.set_button_states(changed_buttons)

                    changed_sliders = {}

                    current_trim_value = elev_trim_slider_value(snap.get(ELEV_TRIM_DATAREF_ID, 0.0))
                    if current_trim_value != last_trim_value:
                        changed_sliders[ELEV_TRIM_SLIDER] = current_trim_value
                        last_trim_value = current_trim_value

                    current_mixture_value = unit_ratio_slider_value(snap.get(MIXTURE_DATAREF_ID, 0.0))
                    if current_mixture_value != last_mixture_value:
                        changed_sliders[MIXTURE_SLIDER] = current_mixture_value
                        last_mixture_value = current_mixture_value

                    current_throttle_value = unit_ratio_slider_value(snap.get(THROTTLE_DATAREF_ID, 0.0))
                    if current_throttle_value != last_throttle_value:
                        changed_sliders[THROTTLE_SLIDER] = current_throttle_value
                        last_throttle_value = current_throttle_value

                    if changed_sliders:
                        matric.set_slider_values(changed_sliders)

                    current_ignition_position = ignition_key_switch_position(snap.get(IGNITION_KEY_ID, 0.0))
                    if current_ignition_position != last_ignition_position:
                        matric.set_switch_positions({IGNITION_KEY_SWITCH: current_ignition_position})
                        last_ignition_position = current_ignition_position

                    current_fuel_warning = fuel_warning_state(snap)
                    if current_fuel_warning != last_fuel_warning:
                        fuel_warning_active, fuel_warning_text = current_fuel_warning
                        matric.set_button_states({FUEL_WARNING_BUTTON: fuel_warning_active})
                        matric.set_button_texts({FUEL_WARNING_BUTTON: fuel_warning_text})
                        last_fuel_warning = current_fuel_warning

                    current_callsign_text = read_tail_number(snap)
                    if current_callsign_text != last_callsign_text:
                        matric.set_button_texts({CALLSIGN_BUTTON: current_callsign_text})
                        last_callsign_text = current_callsign_text

                last_snap = display_snap

            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nStopping background network instance...")
        net_thread.stop()
        net_thread.join(timeout=1.0)
        print("Script stopped by user.")
