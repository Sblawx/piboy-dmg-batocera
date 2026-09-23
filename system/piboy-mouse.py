#!/usr/bin/env python3
"""Joystick -> mouse for the PiBoy DMG.

The Batocera file manager (pcmanfm) shows a cursor but there is no pointing
device on the PiBoy, so the cursor never moves and the desktop is unusable.
This reads the analog stick + buttons from the PiBoy controller and injects a
virtual mouse through /dev/uinput, which Sway sees as a real pointer.

It only injects while the file manager is running (GATE_PROCESS), so it never
interferes with games or EmulationStation navigation, where the same stick is
the gamepad. Launched by the `piboy` service alongside piboy-dmgcontrol.py.

Mapping: stick = move, A = left click, B = right click, dpad = arrow keys.
Under StarCraft only, X/Y/C/Z/L/R also send game keyboard shortcuts.
"""

import os
import select
import signal
import subprocess
import sys
import time

import evdev
from evdev import ecodes, UInput, InputDevice

CONTROLLER_NAME = "PiBoy DMG Controller"

# Only act while one of these processes is running. pcmanfm = the file manager;
# StarCraft.exe = StarCraft under box64/Wine (its /proc comm, <=15 chars). Set to
# an empty tuple to make the mouse always active.
GATE_PROCESSES = ("pcmanfm", "StarCraft.exe")
# The gate check walks every /proc/<pid>/comm, so it is the daemon's main idle
# cost. Measured at 3.8% of a core at 0.7s, which also kept the CPU off its
# lowest OPP. Detecting a launch 2.5s late is harmless (the app takes longer
# than that to appear); we poll faster once something is running so that a close
# is noticed promptly.
GATE_POLL_IDLE_S = 2.5
GATE_POLL_ACTIVE_S = 1.0
# Controller fd wait when no gated app runs: events are discarded anyway, so
# waking 2x/s instead of 5x/s costs nothing and idles deeper.
IDLE_SELECT_S = 0.5

# Launch modes. Preferred: the app launchers (starcraft.sh, FileManager.sh)
# start this daemon on demand and kill it when the app exits, so it costs
# exactly nothing while idle - no process at all, no /proc scanning. "gated" is
# the legacy self-polling fallback, kept so running it bare still works.
#   --starcraft   : always on, StarCraft keyboard shortcuts enabled
#   --filemanager : always on, shortcuts disabled
MODE = "gated"
if "--starcraft" in sys.argv:
    MODE = "starcraft"
elif "--filemanager" in sys.argv:
    MODE = "filemanager"

_running = True

# The pointer/clicks/arrows work under any gated app, but the StarCraft keyboard
# shortcuts (KEY_BTN_MAP) must only fire under StarCraft - never in pcmanfm.
SC_PROCESS = "StarCraft.exe"

# Stick -> pointer response. Tuned slower/finer for aiming in games: half the
# old top speed and a stronger expo so small stick moves crawl the cursor.
CENTER = 127
# The driver already snaps |raw-CENTER| < 30 to CENTER (measured 2026-07-27:
# values 98..156 are never reported), so anything below 30 here was dead code.
# Matching the hardware floor means the expo curve starts from the first value
# that actually exists instead of from a fictional one, and the cursor cannot
# creep on the first live step.
DEADZONE = 30
MAX_SPEED = 5.0           # pixels per tick at full deflection
TICK_S = 0.012            # ~80 Hz pointer updates when active
EXPO = 2.6                # >1 gives fine control near centre, fast at the edge

# Controller source -> mouse button.
BTN_MAP = {
    ecodes.BTN_A: ecodes.BTN_LEFT,
    ecodes.BTN_B: ecodes.BTN_RIGHT,
}

# PiBoy face/shoulder buttons -> StarCraft keyboard shortcuts. Press/release is
# mirrored, so modifiers (Shift/Ctrl) work while held and command keys act as
# taps. Only active while a gated app runs (see GATE_PROCESSES). Physical labels
# on this PiBoy: X=BTN_NORTH, Y=BTN_WEST, C=BTN_C, Z=BTN_Z, L=BTN_TL, R=BTN_TR.
KEY_BTN_MAP = {
    ecodes.BTN_NORTH: ecodes.KEY_A,          # X -> Attack (attack-move)
    ecodes.BTN_WEST: ecodes.KEY_S,           # Y -> Stop
    ecodes.BTN_C: ecodes.KEY_H,              # C -> Hold position
    ecodes.BTN_Z: ecodes.KEY_ENTER,          # Z -> chat / confirm
    ecodes.BTN_TL: ecodes.KEY_LEFTSHIFT,     # L -> Shift (queue / add to selection)
    ecodes.BTN_TR: ecodes.KEY_LEFTCTRL,      # R -> Ctrl (select all of type)
}
SC_KEYS = list(KEY_BTN_MAP.values())

# Press these two together to close the file manager (no keyboard to alt-F4).
QUIT_COMBO = (ecodes.BTN_SELECT, ecodes.BTN_START)

# Dpad (hat) -> arrow keys, for list navigation in the file manager. Sway
# generates key repeat while the arrow is held down.
HAT_KEYS = {
    (ecodes.ABS_HAT0X, -1): ecodes.KEY_LEFT,
    (ecodes.ABS_HAT0X, 1): ecodes.KEY_RIGHT,
    (ecodes.ABS_HAT0Y, -1): ecodes.KEY_UP,
    (ecodes.ABS_HAT0Y, 1): ecodes.KEY_DOWN,
}
ARROW_KEYS = [ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_LEFT, ecodes.KEY_RIGHT]


def log(msg):
    print("[piboy-mouse] %s" % msg, flush=True)


def find_controller():
    for path in evdev.list_devices():
        try:
            d = InputDevice(path)
        except OSError:
            continue
        if CONTROLLER_NAME in d.name:
            return d
    return None


def scan_gate():
    """One /proc pass -> (any gated app running, StarCraft running)."""
    if not GATE_PROCESSES:
        return True, False
    any_open = sc = False
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % pid) as f:
                comm = f.read().strip()
        except OSError:
            continue
        if comm in GATE_PROCESSES:
            any_open = True
            if comm == SC_PROCESS:
                sc = True
    return any_open, sc


def curve(raw):
    """Stick axis value (0..255) -> signed pixels/tick, with deadzone + expo."""
    off = raw - CENTER
    if abs(off) <= DEADZONE:
        return 0.0
    span = 255 - CENTER
    norm = (abs(off) - DEADZONE) / float(span - DEADZONE)
    norm = max(0.0, min(1.0, norm))
    speed = (norm ** EXPO) * MAX_SPEED
    return speed if off > 0 else -speed


def main():
    def on_signal(signum, _frame):
        # The launchers kill us on app exit; handle it so the finally block
        # still releases held buttons/keys and closes the uinput device.
        global _running
        _running = False

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    dev = None
    while _running and dev is None:
        dev = find_controller()
        if dev is None:
            log("controller not found, retrying...")
            time.sleep(3)
    if dev is None:
        return 0

    ui = UInput(
        {
            ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL],
            ecodes.EV_KEY: [ecodes.BTN_LEFT, ecodes.BTN_RIGHT, ecodes.BTN_MIDDLE]
            + ARROW_KEYS + SC_KEYS,
        },
        name="PiBoy Virtual Mouse",
    )
    if MODE == "gated":
        log("started (mode=gated, polling for %s)" % ",".join(GATE_PROCESSES))
    else:
        log("started (mode=%s, on-demand, shortcuts=%s)"
            % (MODE, "on" if MODE == "starcraft" else "off"))

    ax = ay = CENTER
    # On-demand modes are live immediately; only "gated" has to discover it.
    active = MODE != "gated"
    sc_active = MODE == "starcraft"
    held = set()            # mouse buttons + shortcut keys currently pressed
    combo = set()           # QUIT_COMBO source buttons currently held
    hat_key = {ecodes.ABS_HAT0X: None, ecodes.ABS_HAT0Y: None}  # arrow held per axis
    accum_x = accum_y = 0.0
    last_gate = 0.0

    def release_arrows():
        for axis, key in hat_key.items():
            if key is not None:
                ui.write(ecodes.EV_KEY, key, 0)
                hat_key[axis] = None
        ui.syn()

    try:
        while _running:
            now = time.monotonic()

            if MODE == "gated" and now - last_gate >= (
                    GATE_POLL_ACTIVE_S if active else GATE_POLL_IDLE_S):
                last_gate = now
                new_active, new_sc = scan_gate()
                if new_active != active:
                    active = new_active
                    log("gated app %s -> input %s"
                        % ("opened" if active else "closed",
                           "on" if active else "off"))
                    if not active:
                        # Release anything held so nothing can get stuck.
                        for b in list(held):
                            ui.write(ecodes.EV_KEY, b, 0)
                        held.clear()
                        ui.syn()
                        release_arrows()
                if new_sc != sc_active:
                    sc_active = new_sc
                    if not sc_active:
                        # StarCraft gone: drop any held shortcut keys.
                        for k in SC_KEYS:
                            if k in held:
                                ui.write(ecodes.EV_KEY, k, 0)
                                held.discard(k)
                        ui.syn()

            # Drain controller events (non-blocking via select). When idle we
            # poll slowly to stay off the CPU; when active we run at tick rate.
            r, _, _ = select.select([dev.fd], [], [],
                                    TICK_S if active else IDLE_SELECT_S)
            if r:
                try:
                    for ev in dev.read():
                        if ev.type == ecodes.EV_ABS:
                            if ev.code == ecodes.ABS_X:
                                ax = ev.value
                            elif ev.code == ecodes.ABS_Y:
                                ay = ev.value
                            elif ev.code in (ecodes.ABS_HAT0X, ecodes.ABS_HAT0Y) and active:
                                # Dpad -> arrow key press/release.
                                newkey = HAT_KEYS.get((ev.code, ev.value))
                                prev = hat_key[ev.code]
                                if newkey != prev:
                                    if prev is not None:
                                        ui.write(ecodes.EV_KEY, prev, 0)
                                    if newkey is not None:
                                        ui.write(ecodes.EV_KEY, newkey, 1)
                                    hat_key[ev.code] = newkey
                                    ui.syn()
                        elif ev.type == ecodes.EV_KEY and active:
                            # SELECT+START -> close the file manager.
                            if ev.code in QUIT_COMBO:
                                if ev.value:
                                    combo.add(ev.code)
                                else:
                                    combo.discard(ev.code)
                                if len(combo) == len(QUIT_COMBO):
                                    log("quit combo -> closing gated app")
                                    # Popen (not run): wineserver -k can take a
                                    # second under box64, don't freeze the loop.
                                    subprocess.Popen(
                                        ["sh", "-c",
                                         "pkill -x pcmanfm; "
                                         "/userdata/system/wine/wine-quit.sh"],
                                        stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL)
                                    combo.clear()
                            target = BTN_MAP.get(ev.code)
                            if target is not None:
                                if ev.value:            # press/repeat
                                    if target not in held:
                                        ui.write(ecodes.EV_KEY, target, 1)
                                        held.add(target)
                                        ui.syn()
                                else:                   # release
                                    if target in held:
                                        ui.write(ecodes.EV_KEY, target, 0)
                                        held.discard(target)
                                        ui.syn()

                            # StarCraft keyboard shortcuts (X/Y/C/Z/L/R). Only
                            # while StarCraft runs, never in pcmanfm. Press and
                            # release mirrored so Shift/Ctrl hold, commands tap.
                            key = KEY_BTN_MAP.get(ev.code)
                            if key is not None and sc_active:
                                if ev.value:
                                    if key not in held:
                                        ui.write(ecodes.EV_KEY, key, 1)
                                        held.add(key)
                                        ui.syn()
                                else:
                                    if key in held:
                                        ui.write(ecodes.EV_KEY, key, 0)
                                        held.discard(key)
                                        ui.syn()
                except OSError:
                    pass

            if not active:
                continue

            # Pointer motion. Accumulate fractional pixels so slow moves work.
            accum_x += curve(ax)
            accum_y += curve(ay)
            dx, dy = int(accum_x), int(accum_y)
            moved = False
            if dx:
                ui.write(ecodes.EV_REL, ecodes.REL_X, dx)
                accum_x -= dx
                moved = True
            if dy:
                ui.write(ecodes.EV_REL, ecodes.REL_Y, dy)
                accum_y -= dy
                moved = True

            if moved:
                ui.syn()
    except KeyboardInterrupt:
        pass
    finally:
        for b in list(held):
            ui.write(ecodes.EV_KEY, b, 0)
        release_arrows()
        ui.close()


if __name__ == "__main__":
    sys.exit(main())
