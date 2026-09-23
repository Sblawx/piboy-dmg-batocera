#!/usr/bin/env python3
"""PiBoy DMG control daemon for Batocera.

Sits on top of the xpi_gamecon kernel module, which publishes the PiBoy's
MCU state under /sys/kernel/xpi_gamecon/. Provides:

  battery  - mirrors the driver into a standard /sys/class/power_supply/BAT0
             node. The stock Batocera EmulationStation is not built with the
             PIBOY patch, so it only ever looks in /sys/class/power_supply;
             that directory is empty on this board, hence no gauge. Publishing
             a conventional node there gets the native ES gauge back without
             recompiling anything.
  fan      - temperature curve. The driver's `fan` node is a 0-255 PWM duty,
             NOT a percentage.
  volume   - volume wheel -> system volume.
  power    - power switch and low battery -> clean shutdown.
"""

import configparser
import math
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.request

# ---------------------------------------------------------------- config ----
POLL_S = 0.2              # main loop period
# EmulationStation re-reads the battery every 5s (BatteryLevelWatcher::
# updateTime), so publishing much faster than that buys nothing visible. Half a
# second keeps our own staleness off the critical path for a handful of
# syscalls against a tmpfs.
BATT_PUBLISH_S = 0.5      # how often to refresh the power_supply node
FAN_UPDATE_S = 2.0        # how often to re-evaluate the fan curve
SWITCH_DEBOUNCE_S = 1.5   # power switch must be held this long
LOW_BATT_PCT = 5          # clean shutdown below this charge
LOW_BATT_MV = 3150        # terminal-voltage floor, protects the cell
LOW_BATT_WARN_PCT = (10, 7)  # on-screen warnings while discharging
RA_CMD_ADDR = ("127.0.0.1", 55355)   # RetroArch network commands (network_cmd_enable)
OSD_MSG = "/tmp/piboy-osd.msg"       # message channel read by piboy-osd
OSD_CONF = "/userdata/system/piboy-osd.conf"
SAVES_DIR = "/userdata/saves"
SAVE_WAIT_S = 8.0                    # how long to wait for RetroArch's save state

# --------------------------------------------------------------- battery ----
# The MCU has no coulomb counter: its `percent` is a straight linear map of the
# *terminal* voltage, so it collapses under load and jumps when the charger is
# plugged in (measured: 70% -> 26% the instant a 2A load appeared, at an
# unchanged real charge).
#
# This daemon used to publish a linear map of the IR-corrected terminal voltage
# (3345mV=0%, 4080mV=100%, R=45mOhm). Replayed against the calibration log that
# gauge had an rms error of 6.5 points and swung 15 points *upwards* mid
# discharge (07-17: 11.8% -> 21.4% -> 16.6% -> dead), because:
#   - a LiPo's OCV curve is flat between ~3.60 and ~3.70V, where roughly a third
#     of the pack's charge sits. Mapping it linearly makes the gauge crawl in
#     the middle and then collapse over the last few hundred mAh.
#   - pack resistance is not constant. Measured here from 20s load steps in the
#     log, it runs 50 mOhm at full to 120 mOhm near empty, so a fixed 45 mOhm
#     under-corrects badly exactly where accuracy matters. Correcting it
#     properly, though, *amplifies* current noise: at 120 mOhm a 2A load spike
#     moves the inferred OCV by 240mV. Voltage alone cannot be both accurate
#     and steady on this pack.
#
# So the gauge is a hybrid, the way a real fuel-gauge IC works: charge is
# tracked by integrating current (smooth, monotonic, right *rate*), and the
# slow drift of that integration is corrected by pulling it towards the OCV
# curve (right *absolute value*). Replayed over the same log: rms error 1.5
# points, worst 5.3, and sample-to-sample jitter drops from 0.84 to 0.14 points.
#
# Capacity measured by coulomb-counting the 07-20 deep discharge: 3515 mAh
# between OCV 4047 and 3489 mV, plus ~300 mAh above and ~180 mAh below that
# span. The 4900 mAh on the cell label is never reached - the charger tops out
# around 4.12V, not 4.20V.
BATT_CAPACITY_MAH = 4000.0
# OCV (mV) -> state of charge (%). Shape measured on this pack by coulomb-
# counting the 07-20 deep discharge; 0% is pinned at the 3400mV cell floor and
# 100% at 4100mV, the highest rested voltage the charger actually delivers.
# Because it comes from a coulomb count, 1% is ~40 mAh *everywhere*: at a steady
# load the gauge now falls at a steady rate, which the old linear map did not.
BATT_OCV_TABLE = (
    (3400, 0.0), (3489, 4.6), (3520, 9.0), (3549, 13.4), (3569, 17.8),
    (3593, 22.2), (3609, 26.6), (3625, 31.0), (3644, 35.4), (3661, 39.8),
    (3680, 44.2), (3704, 48.6), (3731, 53.0), (3762, 57.4), (3794, 61.7),
    (3826, 66.1), (3861, 70.5), (3901, 74.9), (3942, 79.3), (3980, 83.7),
    (4014, 88.1), (4047, 92.5), (4100, 100.0),
)
# State of charge (%) -> pack resistance (mOhm), from dV/dI across 20s load
# steps in the log, binned by depth of discharge. Rises as the pack empties.
BATT_R_TABLE = ((0, 140), (10, 120), (25, 100), (50, 80), (75, 55), (100, 50))
# Time constant of the pull towards the OCV curve. Slower on the way up, so the
# gauge does not visibly climb while the console is draining it - but not too
# slow, or a pessimistic seed can never be walked back and the console shuts
# down with charge to spare. Replaying 07-17 (console really died at 22:37,
# seeded 13 points low): 600s reads 0% never, 1800s at 22:27, 6000s at 21:44,
# i.e. 53 minutes of playing time thrown away. 1800s keeps the safety margin
# without the waste, at the cost of two 1-point rises over three hours.
BATT_ANCHOR_TAU_S = 600.0
BATT_ANCHOR_TAU_UP_S = 1800.0
# The OCV curve is only meaningful under a settled load. Ignore charging (the
# charger's polarisation puts the terminal ~100mV above OCV, which would read
# as a full pack) and ignore samples straddling a load step.
BATT_ANCHOR_MIN_MA = 200
BATT_ANCHOR_STEP_MA = 300
# Seed the integrator from the OCV curve over the first few seconds. A single
# sample is worth +-5 points, a short median is worth about +-2.
BATT_SEED_S = 10.0
# ...but only while no real current is flowing. What ruins a voltage estimate
# is the IR term plus polarisation, and both scale with the current: measured
# 2026-07-28, ten minutes of charging at ~1A (160mAh into a 4000mAh pack, i.e.
# 4 real points) lifted the terminal by 200mV, worth ~14 points on the OCV
# table. So while a charge is genuinely flowing the last saved count is the
# better starting value; at a taper current it is the other way round, and the
# terminal wins outright - see BATT_OCV_TRUST_MA.
BATT_STATE_MAX_AGE_S = 48 * 3600
# Below this current the IR term is negligible - 200mA against the 250mOhm
# ceiling is 50mV - so the terminal can be read as the open-circuit voltage
# directly, and that beats any saved value. This is the normal end-of-charge
# state, which is precisely where the old "on the charger, distrust the
# voltage" rule did the most damage.
BATT_OCV_TRUST_MA = 200
# Charge termination: the pack is full when the charger has tapered off while
# holding the terminal high. This is the one moment the absolute charge is
# known exactly, so it re-zeroes the integrator's accumulated drift.
BATT_FULL_MV = 4050
BATT_FULL_TAPER_MA = 150
BATT_FULL_HOLD_S = 60.0
# Charge-hold abandons if the pack is not actually filling. VBus on its own is
# NOT proof of a charger: on 2026-08-07 bit 0x80 stayed asserted for over an
# hour with nothing plugged in at all. Without this, a low-battery shutdown
# parks the console "charging" with the screen off and runs the cell flat
# instead of powering down - the one failure mode that can damage the pack.
CHARGE_HOLD_GIVEUP_S = 120.0
# Reject obviously broken reads rather than feeding them to the integrator; the
# log shows occasional 0mV and 4200mV glitch samples from the MCU.
BATT_SANE_MIN_MV = 2500
BATT_SANE_MAX_MV = 4400
# Last known state of charge, written at shutdown and periodically. Two uses:
# a seed fresher than BATT_STATE_FRESH_S carries the coulomb count across a
# quick reboot (the OCV re-seed is poor while the charger clamps the terminal),
# and a stale one lets the boot log say how much the pack gained or lost while
# the console was off - which is how the "charged all night but woke up empty"
# failure of 2026-07-27/28 was only diagnosable from raw CSV timestamps.
BATT_STATE_PATH = "/userdata/system/piboy-battery.state"
BATT_STATE_FRESH_S = 1800
BATT_STATE_SAVE_S = 600.0
# How long after the charger last pushed current into the pack before the
# terminal voltage means anything again. Polarisation and surface charge decay
# over minutes, and until they have, the terminal reads high.
#
# This replaces gating on VBus, which was the bug behind the 2026-08-07 "gauge
# says 28% on a pack at ~75%" failure. The reason to distrust the voltage is
# that the charger is *delivering*, not that a cable is plugged in - and on
# this console those are very different, because the charger regularly drops
# out while VBus stays asserted. Gating on VBus meant that as long as the cable
# was in, the anchor never ran, so nothing could ever correct the coulomb
# count. It drifted for hours, survived every restart through the state file,
# and the daemon logged the contradiction without acting on it: "seeding from
# state file: 29.1% saved 0min ago (voltage guess was 91.5%)".
BATT_RELAX_S = 600.0
# Gross-error guard. V_terminal = OCV + I*R, so whichever way the current
# flows, one side of that is a bound on the true charge no matter what R is,
# and bounding R from above gives the other side. This brackets the pack
# without trusting the R table at all, which is the point: it is the check that
# still works when the rest of the model is wrong.
#
# 250mOhm is deliberately past everything the log supports (median of 99 clean
# current steps: 119mOhm; per-session fits: 100-300; the 17:59->18:00 charge to
# discharge swing on 08-07: 165), so the bracket is wide and only ever catches
# nonsense. The margin covers surface charge holding the terminal high just
# after a charge: the worst excursion across the two sessions that start on a
# known-full pack is 7 points, while the failure this exists to catch was 34.
BATT_R_MAX_MOHM = 250.0
BATT_BRACKET_MARGIN = 12.0
BATT_BRACKET_LOG_S = 300.0
# ...but only once the bracket has been violated continuously for this long.
# The MCU emits the odd nonsense sample, and a single spurious +1100mA in the
# middle of a discharge collapses the ceiling and would yank the gauge down by
# ten points: replaying 07-27 with no hold does exactly that at 13:31. Real
# errors persist, glitches do not.
BATT_BRACKET_HOLD_S = 30.0

# Raw sample logging, for building a calibrated OCV-vs-SoC table offline. Time
# + signed current lets a coulomb count be integrated afterwards, so the SoC
# axis does not depend on voltage. Coarse interval keeps SD wear negligible.
BATT_LOG = True
BATT_LOG_PATH = "/userdata/system/piboy-battery-log.csv"
BATT_LOG_S = 20
BATT_LOG_MAX_BYTES = 5 * 1024 * 1024

# Fan duty is 0-255 (0-100%), NOT a percentage. Behaviour is chosen by a named
# profile in FAN_CONF_PATH (see FAN_PROFILES). The file is re-read live, so the
# profile can be changed while the console runs. Regardless of profile, the fan
# is forced to full above FAN_CRITICAL_C so no setting can cook the SoC (the
# Pi4 soft-throttles at 80C).
# The bicolour POWER LED (not the screen). Both channels are 0-255 and are
# re-sent to the MCU every frame by the driver, so a write sticks until reboot
# only - hence this config, applied at start and live-reloaded like the fan.
# 2/2 is the driver's own resting value, i.e. what the console looks like
# stock, so it is the default here too.
LED_CONF_PATH = "/userdata/system/piboy-led.conf"
LED_DEFAULT_RED = 2
LED_DEFAULT_GREEN = 2
LED_DEFAULT_MODE = "static"     # static = red/green fixes ; battery = indicateur
# Couleurs de l'indicateur batterie (0-255 par canal). Volontairement moderees
# (the LED is bright: 2/2 = factory idle value). Adjust if needed.
LED_BATT_GREEN = 50             # > 50% : vert
LED_BATT_AMBER = (50, 22)       # 15-50% : ambre (rouge, vert)
LED_BATT_RED   = 60             # < 15% : rouge

FAN_CONF_PATH = "/userdata/system/piboy-fan.conf"
FAN_DEFAULT_PROFILE = "quiet"
FAN_HYST_C = 2.0
FAN_CRITICAL_C = 80
FAN_CRITICAL_DUTY = 255

# name -> (idle_duty, [(temp_c, duty_0_255), ...]). Curves are anchored on Pi4
# thermals: passive idle is fine into the 60s, throttling starts ~80C.
FAN_PROFILES = {
    # dead silent at rest, only spins up when it genuinely has to
    "silent":   (0,  [(68, 110), (74, 180), (80, 255)]),
    # off at idle, gentle ramp - quiet but keeps a margin
    "quiet":    (0,  [(60, 80), (67, 120), (73, 175), (79, 235)]),
    # always turning a little, comfortable temps
    "balanced": (60, [(52, 80), (60, 120), (67, 160), (74, 210), (80, 255)]),
    # the vendor fan.ini curve - coolest, loudest
    "cool":     (75, [(50, 75), (55, 90), (60, 110), (65, 147), (70, 194), (75, 242)]),
}

# ----------------------------------------------------------------- paths ----
XPI = "/sys/kernel/xpi_gamecon"
PSDIR = "/sys/class/power_supply"
BAT = os.path.join(PSDIR, "BAT0")
THERMAL = "/sys/class/thermal/thermal_zone0/temp"

_running = True


def log(msg):
    print("[piboy] %s %s" % (time.strftime("%m-%d %H:%M:%S"), msg), flush=True)


def read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def write_str(path, value):
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return True
    except OSError:
        return False


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


def pin_builtin_audio_sink():
    """Keep the volume wheel aimed at the real speaker/jack.

    batocera-audio setSystemVolume acts on pipewire's @DEFAULT_SINK@, but any
    USB audio device (the M8 headless, most notably) grabs default the instant
    it enumerates -- wireplumber's newest-device-wins policy. Without this,
    turning the wheel while such a device is attached silently redirects the
    volume change into that device instead of bcm2835 Headphones, and
    EmulationStation's own audio drifts out of sync with the wheel too, since
    it plays through the same default sink. m8c/m8c.sh does the same pin at
    launch time; this covers the wheel at all other times (idle in ES, etc).
    Only called when the wheel actually moves, so the extra subprocess cost is
    rare, not per poll cycle.
    """
    try:
        env = dict(os.environ, XDG_RUNTIME_DIR="/var/run")
        out = subprocess.run(["wpctl", "status"], env=env, capture_output=True,
                              text=True, timeout=2).stdout
        for line in out.splitlines():
            if "Built-in Audio Stereo" in line:
                # wpctl prefixes lines with a box-drawing char that \s does not
                # match -- take the first number on the line, not an anchored one.
                m = re.search(r"\d+", line)
                if m:
                    subprocess.run(["wpctl", "set-default", m.group()], env=env,
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=2)
                break
    except (OSError, subprocess.SubprocessError):
        pass  # best-effort; never let this break the volume wheel


# ------------------------------------------------------------------ fan ----
def load_fan_config():
    """Return (profile_name, idle_duty, curve) from FAN_CONF_PATH.

    The file selects a named profile, or gives a custom curve. Anything
    missing or malformed falls back to FAN_DEFAULT_PROFILE so the fan always
    has a sane behaviour.
    """
    profile = FAN_DEFAULT_PROFILE
    custom_curve = None
    custom_idle = None
    try:
        if os.path.isfile(FAN_CONF_PATH):
            cp = configparser.ConfigParser()
            cp.read(FAN_CONF_PATH)
            sec = cp["fan"] if cp.has_section("fan") else {}
            profile = sec.get("profile", FAN_DEFAULT_PROFILE).strip().lower()
            if "idle_duty" in sec:
                custom_idle = max(0, min(255, int(sec["idle_duty"])))
            if sec.get("curve", "").strip():
                # "60:80, 70:160, 80:255"
                pairs = []
                for chunk in sec["curve"].replace(";", ",").split(","):
                    t, d = chunk.split(":")
                    pairs.append((int(t), max(0, min(255, int(d)))))
                if pairs:
                    custom_curve = sorted(pairs)
    except (OSError, KeyError, ValueError) as e:
        log("fan config unreadable (%s), using '%s'" % (e, FAN_DEFAULT_PROFILE))
        profile = FAN_DEFAULT_PROFILE

    if profile == "custom" and custom_curve:
        idle = custom_idle if custom_idle is not None else 0
        return "custom", idle, custom_curve
    if profile not in FAN_PROFILES:
        log("fan profile '%s' unknown, using '%s'" % (profile, FAN_DEFAULT_PROFILE))
        profile = FAN_DEFAULT_PROFILE
    idle, curve = FAN_PROFILES[profile]
    if custom_idle is not None:
        idle = custom_idle
    return profile, idle, list(curve)


def load_led_config():
    """Return (red, green) brightness for the power LED from LED_CONF_PATH.

    `red` and `green` are the two channels of the bicolour power LED (they are
    NOT the screen - brightness is the mechanical wheel). The driver keeps
    whatever it was last told and re-sends it to the MCU on every frame, so a
    single write sticks until reboot; this file is what makes it survive one.
    The kernel does not clamp - it stores the raw int and the MCU interprets it
    - so the clamping to 0-255 happens here.
    """
    mode = LED_DEFAULT_MODE
    red, green = LED_DEFAULT_RED, LED_DEFAULT_GREEN
    try:
        if os.path.isfile(LED_CONF_PATH):
            cp = configparser.ConfigParser()
            cp.read(LED_CONF_PATH)
            sec = cp["led"] if cp.has_section("led") else {}
            mode = sec.get("mode", mode).strip().lower()
            red = int(sec.get("red", red))
            green = int(sec.get("green", green))
    except Exception as exc:
        log("led config unreadable (%s), using defaults" % exc)
        mode, red, green = LED_DEFAULT_MODE, LED_DEFAULT_RED, LED_DEFAULT_GREEN
    if mode not in ("static", "battery"):
        mode = LED_DEFAULT_MODE
    red = max(0, min(255, red))
    green = max(0, min(255, green))
    return mode, red, green


def battery_led(percent, charging, t):
    """Couleur (red, green) de l'indicateur batterie.

    Green > 50%, amber 15-50%, red < 15%; while charging the LED pulses gently.
    """
    if percent > 50:
        r, g = 0, LED_BATT_GREEN
    elif percent >= 15:
        r, g = LED_BATT_AMBER
    else:
        r, g = LED_BATT_RED, 0
    if charging:
        k = 0.2 + 0.8 * (0.5 + 0.5 * math.sin(t * 2.0))   # pulsation ~3 s
        r, g = int(r * k), int(g * k)
    return max(0, min(255, r)), max(0, min(255, g))


def apply_led(red, green):
    """Write both LED channels, but only where the node disagrees.

    Same reasoning as the fan: compare against the node rather than a
    remembered value, so we converge back if anything else wrote to it.
    """
    for node, value in (("red", red), ("green", green)):
        path = os.path.join(XPI, node)
        if read_int(path) != value:
            write_str(path, value)


def target_duty(curve, idle_duty, temp_c, prev_band):
    """Pick a fan duty for `temp_c`, with hysteresis on the way down.

    Returns (band, duty). Band 0 is "below the first threshold"; band N is
    curve[N-1]. Stepping up is immediate, stepping down only happens once the
    temperature is clearly below the band we were in, so a reading sitting on
    a threshold does not make the fan surge up and down. A hard critical
    override sits above any profile.
    """
    if temp_c >= FAN_CRITICAL_C:
        return len(curve), FAN_CRITICAL_DUTY
    band = 0
    for i, (threshold, _duty) in enumerate(curve, start=1):
        if temp_c >= threshold:
            band = i
    if prev_band and band < prev_band and prev_band <= len(curve):
        if temp_c > curve[prev_band - 1][0] - FAN_HYST_C:
            band = prev_band
    duty = idle_duty if band == 0 else curve[band - 1][1]
    return band, max(0, min(255, duty))


# -------------------------------------------------------------- battery ----
def power_supply_ready():
    """Overmount PSDIR with a tmpfs we can write into.

    sysfs itself is not writable from userspace, but PSDIR is an empty
    directory on this board, so shadowing it with a tmpfs hides nothing.
    """
    if os.path.isdir(BAT):
        return True
    try:
        existing = os.listdir(PSDIR)
    except OSError:
        log("battery: %s missing, not publishing" % PSDIR)
        return False
    if existing:
        # A real power supply showed up; leave the kernel's version alone.
        log("battery: %s not empty (%s), not publishing" % (PSDIR, existing))
        return False
    rc = subprocess.run(
        ["mount", "-t", "tmpfs", "-o", "size=64k", "tmpfs", PSDIR],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    if rc.returncode != 0:
        log("battery: tmpfs mount failed: %s" % rc.stderr.decode().strip())
        return False
    os.makedirs(BAT, exist_ok=True)
    write_str(os.path.join(BAT, "type"), "Battery\n")
    write_str(os.path.join(BAT, "present"), "1\n")
    write_str(os.path.join(BAT, "technology"), "Li-ion\n")
    log("battery: publishing at %s" % BAT)
    return True


_soc = None               # the integrator: charge left, in percent
_seed = None              # (deadline, [samples]) while seeding from OCV
_batt_last = None         # (monotonic, milliamps) of the previous update
_full_since = None        # when the charge-termination condition first held
_last_charge = None       # monotonic time the charger last pushed current in
_bracket_logged = 0.0     # monotonic time the bracket guard last said so
_bracket_since = None     # when the bracket was first violated, or None


def _interp(x, table):
    """Linear interpolation over a sorted ((x, y), ...) table, clamped."""
    if x <= table[0][0]:
        return table[0][1]
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return table[-1][1]


def soc_from_ocv(open_circuit):
    return max(0.0, min(100.0, _interp(open_circuit, BATT_OCV_TABLE)))


def pack_resistance(soc):
    """Ohms. Depends on how full the pack is, hence on the value we want."""
    return _interp(soc, BATT_R_TABLE) / 1000.0


def ocv_soc(millivolts, milliamps, charger_active=False):
    """State of charge implied by the voltage alone.

    V_terminal = OCV + I*R, but R itself depends on the state of charge, so
    solve it by fixed point. On battery the correction is positive while R
    falls as the estimate rises, so the iteration damps itself and settles in
    two or three passes. While the charger is delivering both of those signs
    flip and it runs away downwards instead - it seeded a pack sitting at ~18%
    as 8% - so on that branch take a single pass.

    `charger_active` used to be passed VBus, which meant a single pass any time
    a cable was plugged in, even with the charger delivering nothing. That
    under-corrects R and so reads low, which is one of the ways the 08-07 gauge
    failure stayed invisible. It wants the *delivering* question.
    """
    soc = soc_from_ocv(millivolts)
    for _ in range(1 if (charger_active or milliamps >= 0) else 3):
        soc = soc_from_ocv(millivolts - milliamps * pack_resistance(soc))
    return soc


def soc_bracket(millivolts, milliamps):
    """(floor, ceiling) on the true state of charge, from the terminal alone.

    Uses only that the pack's resistance lies somewhere in 0..BATT_R_MAX_MOHM.
    Charging, the terminal sits above OCV, so the plain reading is a ceiling
    and the R-corrected one a floor; discharging, the two swap. Nothing here
    depends on BATT_R_TABLE being right.
    """
    shifted = soc_from_ocv(millivolts - milliamps * BATT_R_MAX_MOHM / 1000.0)
    plain = soc_from_ocv(millivolts)
    return (shifted, plain) if milliamps > 0 else (plain, shifted)


def bracket_clamp(soc, millivolts, milliamps):
    """(corrected soc, floor, ceiling). Unchanged when it was already possible.

    The margin is applied outwards on both sides, and the low edge wins if they
    ever cross, so a nearly-full pack whose ceiling sits below the floor+margin
    is pushed up rather than down.
    """
    floor, ceiling = soc_bracket(millivolts, milliamps)
    low = floor - BATT_BRACKET_MARGIN
    high = max(ceiling + BATT_BRACKET_MARGIN, low)
    return min(max(soc, low), high), floor, ceiling


def vbus_present():
    """Bit 0x80 of `status`. NOT proof that a charger is attached.

    It was measured clean once (0x46 on battery, 0xc6 plugged, 2026-07-27) and
    trusted on that basis. It is not trustworthy: on 2026-08-07 it stayed
    asserted for over an hour with the cable physically unplugged, and it had
    cleared correctly on 07-28, so the fault is intermittent. Treat it as a
    hint. The only proof that a charger is present is charge arriving, i.e. a
    positive `amps` - which is what every decision in this file now keys on.
    """
    status = read_int(os.path.join(XPI, "status"))
    return bool(status is not None and status & 0x80)


def save_battery_state():
    if _soc is not None:
        # Second number is the coulomb count, third the raw OCV reading of the
        # same moment. Both are needed: the count is the better estimate, but
        # only the OCV can be compared against the OCV taken at the next boot.
        # Differencing a drifted coulomb count against a fresh OCV guess is how
        # 2026-07-28 reported "+5.4 points while off" for a pack that had in
        # fact lost ~70mV of open-circuit voltage sitting on the charger.
        mv = read_int(os.path.join(XPI, "battery"))
        ma = read_int(os.path.join(XPI, "amps"))
        # No charger flag needed: ocv_soc already single-passes on a positive
        # current, which is exactly the charger-is-delivering case. Passing
        # VBus here made the saved OCV read low whenever a cable was plugged
        # in, which then poisoned the "what did the off period do" comparison.
        ocv = ("%.1f" % ocv_soc(mv, ma or 0)
               if mv is not None else "")
        write_str(BATT_STATE_PATH,
                  "%d %.1f %s\n" % (int(time.time()), _soc, ocv))


def load_battery_state():
    """(epoch, coulomb_soc, ocv_soc_or_None) from the last run, or None."""
    try:
        with open(BATT_STATE_PATH) as f:
            fields = f.read().split()
        epoch, soc = int(fields[0]), float(fields[1])
        ocv = float(fields[2]) if len(fields) > 2 else None
        return epoch, max(0.0, min(100.0, soc)), ocv
    except (OSError, ValueError, IndexError):
        return None


def log_battery_sample(millivolts, milliamps, temp_mc):
    """Append one raw reading for offline OCV/coulomb-count calibration."""
    try:
        new = not os.path.exists(BATT_LOG_PATH)
        if not new and os.path.getsize(BATT_LOG_PATH) > BATT_LOG_MAX_BYTES:
            # Keep one previous cycle's worth, then start fresh.
            try:
                os.replace(BATT_LOG_PATH, BATT_LOG_PATH + ".1")
            except OSError:
                pass
            new = True
        with open(BATT_LOG_PATH, "a") as f:
            if new:
                f.write("epoch,mV,mA,mcu_pct,temp_mC\n")
            mcu = read_int(os.path.join(XPI, "percent"))
            f.write("%d,%d,%d,%s,%s\n" % (
                int(time.time()), millivolts, milliamps,
                "" if mcu is None else mcu,
                "" if temp_mc is None else temp_mc))
    except OSError:
        pass


def publish_battery():
    """Refresh BAT0. Returns (percent, charging) so callers can act on it."""
    global _soc, _seed, _batt_last, _full_since, _last_charge
    global _bracket_logged, _bracket_since

    millivolts = read_int(os.path.join(XPI, "battery"))
    milliamps = read_int(os.path.join(XPI, "amps"))
    if millivolts is None or not (BATT_SANE_MIN_MV < millivolts < BATT_SANE_MAX_MV):
        # Glitch sample. Keep whatever we last published rather than letting it
        # into the integrator, where the error would never wash out.
        return (None if _soc is None else int(round(_soc))), True
    if milliamps is None:
        milliamps = 0

    # Bit 0x80 of `status` is VBus, and it IS trustworthy: measured 0x46 on
    # battery and 0xc6 with the charger attached (2026-07-27). Comments in this
    # file and in boot-custom.sh used to claim it stayed set on battery and so
    # was unusable - that was simply wrong, and it cost real accuracy, see the
    # anchor below.
    #
    # It is not the same question as `charging` though. The charger on this
    # console cannot cover the console's own draw: measured VBus present with
    # the pack still supplying 550mA. So the sign of `amps` is what says
    # whether the pack is filling or draining, and VBus says whether the
    # terminal voltage can be trusted.
    status = read_int(os.path.join(XPI, "status"))
    on_charger = bool(status is not None and status & 0x80)
    charging = milliamps >= 0
    now = time.monotonic()
    if milliamps > 0:
        _last_charge = now
    # True once the pack has had time to shed the polarisation a charge leaves
    # behind. Everything voltage-derived below is gated on this rather than on
    # VBus, because a charger that is attached but delivering nothing does not
    # disturb the terminal at all - and this console spends hours in that state.
    relaxed = _last_charge is None or now - _last_charge >= BATT_RELAX_S

    # --- seed the integrator from the OCV curve on the first few samples ---
    if _soc is None:
        if _seed is None:
            _seed = (now + BATT_SEED_S, [])
        _seed[1].append(ocv_soc(millivolts, milliamps, not relaxed))
        ordered = sorted(_seed[1])
        median = ordered[len(ordered) // 2]
        if now < _seed[0]:
            # Publish something so ES has a value, but report None to the
            # caller: a single OCV sample is worth +-5 points and must not be
            # allowed to trip the low-battery shutdown.
            write_str(os.path.join(BAT, "capacity"), "%d\n" % int(round(median)))
            write_str(os.path.join(BAT, "status"), "%s\n" % (
                "Discharging" if not on_charger
                else ("Charging" if charging else "Not charging")))
            write_str(os.path.join(BAT, "voltage_now"), "%d\n" % (millivolts * 1000))
            write_str(os.path.join(BAT, "current_now"), "%d\n" % (milliamps * 1000))
            return None, charging
        _soc = median
        _seed = None
        state = load_battery_state()
        age = None if state is None else time.time() - state[0]
        if abs(milliamps) <= BATT_OCV_TRUST_MA:
            # Barely any current, so the terminal IS the open-circuit voltage:
            # even at the 250mOhm ceiling, 200mA is worth 50mV. Nothing beats a
            # direct reading, so this outranks every saved value. It is the
            # normal state at the end of a charge, which is exactly when the
            # old rules went furthest wrong - on 08-08 the pack came back from
            # a night on the charger at 4110mV taking 100mA, i.e. full, and the
            # standby-drain branch below proposed 69.5%.
            log("battery: %dmA is small enough that %dmV is the open-circuit"
                " voltage - taking %.1f%% directly" % (milliamps, millivolts, _soc))
        elif state is not None and 0 <= age <= BATT_STATE_FRESH_S:
            # Same session, near enough: carry the coulomb count across the
            # restart rather than re-guessing it.
            log("battery: seeding from state file: %.1f%% saved %dmin ago"
                " (voltage guess was %.1f%%)" % (state[1], age // 60, _soc))
            _soc = state[1]
        elif state is not None and on_charger and 0 <= age <= BATT_STATE_MAX_AGE_S:
            # A real charge current is flowing, so the terminal is inflated and
            # the saved count is the better starting point. Carried over as-is:
            # there used to be a BATT_STANDBY_DRAIN_MA subtraction here, on the
            # belief that a powered-off console cannot charge. That belief is
            # wrong. Overnight on 08-07/08 the console sat switched off on the
            # charger for 11.4h and its taper current fell from 500mA to 100mA
            # at 4105 -> 4115mV: it gained roughly 450mAh, while the model
            # predicted losing 456mAh. Subtracting a term whose *sign* has been
            # measured backwards is worse than not adjusting at all, so the
            # bracket and the anchor are left to do the correcting.
            _soc = state[1]
            log("battery: charging at %dmA so the terminal is inflated (it said"
                " %.1f%%); resuming the saved %.1f%% from %.1fh ago"
                % (milliamps, median, state[1], age / 3600.0))
        elif state is not None and state[2] is not None:
            # Off the charger the voltage IS the honest estimate, so just take
            # it - but log OCV-then vs OCV-now, the only apples-to-apples
            # measure of what the off period did to the pack. The console does
            # not charge while powered off (proven 07-27 and 07-28), so a
            # negative delta here is expected: it is the MCU's standby drain.
            log("battery: %.1fh off, OCV %.1f%% -> %.1f%% (%+.1f points);"
                " coulomb count was %.1f%%"
                % (age / 3600.0, state[2], _soc, _soc - state[2], state[1]))
        # A saved count is only worth carrying forward if the pack could still
        # be there. On 08-07 this branch accepted 29.1% for a pack the terminal
        # put at ~90%, logged the 62-point contradiction, and carried the wrong
        # value through three restarts because nothing downstream could
        # challenge it while the charger was plugged in.
        #
        # When it is impossible, fall back to the voltage estimate rather than
        # to the edge of the bracket. The bracket edge is only a bound, not an
        # estimate, and starting there leaves the gauge ~20 points low with the
        # anchor's 1800s time constant far too slow to close the gap against a
        # 1A load - measured live on the first deploy of this guard, which sat
        # at 37% for four minutes on a pack at ~57%. The seed is also the one
        # moment the OCV median is at its best: it is a median over several
        # seconds, and by construction we only get here when the state file has
        # already been shown to be wrong.
        clamped, floor, ceiling = bracket_clamp(_soc, millivolts, milliamps)
        if abs(clamped - _soc) > 0.05:
            fallback, _, _ = bracket_clamp(median, millivolts, milliamps)
            log("battery: %.1f%% is impossible at %dmV/%dmA (%.0f-%.0f%%);"
                " starting from the voltage estimate %.1f%% instead"
                % (_soc, millivolts, milliamps, floor, ceiling, fallback))
            _soc = fallback
        log("battery: seeded at %.1f%% (%dmV, %dmA)" % (_soc, millivolts, milliamps))

    # --- integrate current: this is what gives the gauge its rate ---
    if _batt_last is None:
        dt, last_ma = BATT_PUBLISH_S, milliamps
    else:
        dt, last_ma = min(now - _batt_last[0], 5.0), _batt_last[1]
        _soc += (last_ma + milliamps) / 2.0 * dt / 3600.0 / BATT_CAPACITY_MAH * 100.0
    _batt_last = (now, milliamps)

    # --- correct its drift against the OCV curve ---
    # Requires the charger to have stopped delivering, not to be unplugged. The
    # comment here used to claim that a charger too weak to cover the console's
    # draw still clamps the terminal above the pack's real OCV; that cannot be
    # right, because a pack only supplies current when its own EMF exceeds the
    # node it is feeding. What does hold the terminal high is the polarisation
    # left over from charging, and that is what BATT_RELAX_S waits out. A
    # sample straddling a load step is thrown away too - the pack needs a
    # moment to settle before its voltage means anything.
    settled = (relaxed and milliamps < -BATT_ANCHOR_MIN_MA
               and abs(milliamps - last_ma) < BATT_ANCHOR_STEP_MA)
    if settled:
        target = ocv_soc(millivolts, milliamps)
        tau = BATT_ANCHOR_TAU_S if target < _soc else BATT_ANCHOR_TAU_UP_S
        _soc += (1.0 - math.exp(-dt / tau)) * (target - _soc)

    # --- and catch the errors the anchor is far too slow to walk back ---
    # The anchor moves with a 10-30 minute time constant, which is right for
    # drift and useless against a count that is 30 points out - on 08-07 it
    # would have taken over an hour to recover, and only once the charger came
    # off. The bracket is the coarse backstop underneath it: it asks only
    # whether the count is physically possible given the terminal, and snaps it
    # back to the nearest possible value when it is not.
    corrected, floor, ceiling = bracket_clamp(_soc, millivolts, milliamps)
    if abs(corrected - _soc) > 0.05:
        if _bracket_since is None:
            _bracket_since = now
        elif now - _bracket_since >= BATT_BRACKET_HOLD_S:
            if now - _bracket_logged >= BATT_BRACKET_LOG_S:
                log("battery: %.1f%% is outside what %dmV at %dmA allows"
                    " (%.0f-%.0f%%); correcting to %.1f%%"
                    % (_soc, millivolts, milliamps, floor, ceiling, corrected))
                _bracket_logged = now
            _soc = corrected
    else:
        _bracket_since = None

    # --- charge termination: the one point where charge is known exactly ---
    # Keyed on VBus rather than the current sign: at the end of a charge the
    # console's own draw makes the pack current hover around zero and dip
    # slightly negative, which would keep resetting the timer. But the window
    # has to be symmetric - a bare `milliamps <= taper` is also true at -500mA,
    # i.e. a pack that is visibly draining, and it duly latched 100% onto a
    # pack sitting at ~97% and falling. What identifies a finished charge is
    # the current being *small either way* while the charger holds the
    # terminal high: neither filling nor meaningfully draining.
    if (on_charger and millivolts >= BATT_FULL_MV
            and abs(milliamps) <= BATT_FULL_TAPER_MA):
        if _full_since is None:
            _full_since = now
        elif now - _full_since >= BATT_FULL_HOLD_S:
            _soc = 100.0
    else:
        _full_since = None

    _soc = max(0.0, min(100.0, _soc))
    percent = int(round(_soc))

    # "Not charging" is the standard power_supply value for plugged-in-but-not
    # filling, and it is worth surfacing here rather than folding into
    # "Discharging": on this console it is the normal state while playing with
    # the charger attached, and it tells the user that being plugged in is not
    # actually holding the pack up.
    # `relaxed` means nothing has flowed into the pack for BATT_RELAX_S. A pack
    # that is draining and has had nothing arrive for ten minutes is on
    # battery, whatever VBus claims - and VBus does lie: on 08-07 it stayed
    # asserted for over an hour with nothing plugged in, so the gauge sat there
    # reporting "Not charging" on a console that was plainly discharging.
    if not on_charger or (relaxed and milliamps < 0):
        state = "Discharging"
    elif percent >= 100:
        state = "Full"
    elif charging:
        state = "Charging"
    else:
        state = "Not charging"

    write_str(os.path.join(BAT, "capacity"), "%d\n" % percent)
    write_str(os.path.join(BAT, "status"), "%s\n" % state)
    write_str(os.path.join(BAT, "voltage_now"), "%d\n" % (millivolts * 1000))
    write_str(os.path.join(BAT, "current_now"), "%d\n" % (milliamps * 1000))
    write_str(os.path.join(BAT, "current_avg"), "%d\n" % (milliamps * 1000))
    write_str(os.path.join(BAT, "charge_full"),
              "%d\n" % int(BATT_CAPACITY_MAH * 1000))
    write_str(os.path.join(BAT, "charge_now"),
              "%d\n" % int(BATT_CAPACITY_MAH * 1000 * _soc / 100.0))
    return percent, charging


# ----------------------------------------------------------------- power ----
def charge_hold():
    """Park the console to charge instead of powering off.

    Charging is much faster with the Pi running: measured +850..1050mA into the
    pack in this state, i.e. a full charge in ~4h. So a shutdown requested with
    a charger actually delivering stops ES and stays resident instead.

    It used to say here that a powered-off console does not charge AT ALL,
    from two July observations of a pack that came back having lost OCV. That
    is too strong: overnight on 2026-08-07/08 the console sat switched off on
    the charger for 11.4h and its taper current fell from 500mA to 100mA at
    4105 -> 4115mV, i.e. it finished its charge while off. Powered off it
    charges, just slowly; this mode is a speed-up, not the only way.

    ONLY valid while the power slider is still on. With the slider off the MCU
    cuts the rail on its own regardless of what the Pi does - measured 07-28,
    charge-hold engaged 14:47:40 and the last battery sample is 14:48:24, 44
    seconds later. Holding there does not charge, it just turns a clean
    poweroff into a power cut mid-write; the caller must not enter this
    function on the switch path.

    Leaves when the charger is unplugged (-> real poweroff), or when the
    slider is switched off (-> poweroff, the user really wants it off).

    Returns "poweroff" or "exit" (daemon stopped externally).
    """
    subprocess.run(["/etc/init.d/S31emulationstation", "stop"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sync"])
    # Screen off, exactly like the veille standby: with the Pi running (120Hz
    # heartbeat alive) flags=0 only cuts the LCD rail (~440mA), it does NOT
    # let the MCU drop board power. Frees that much charger budget for the
    # pack and makes the console look off, which is what the user asked for.
    write_str(os.path.join(XPI, "flags"), "0")
    fan_band = 0
    next_fan = next_log = next_save = 0.0
    draining_since = None
    while _running:
        if not vbus_present():
            log("charge-hold: charger unplugged")
            return "poweroff"
        status = read_int(os.path.join(XPI, "status"))
        if status is not None and not status & 0x40:
            log("charge-hold: power switch turned off")
            return "poweroff"
        now = time.monotonic()

        # The only thing that proves a charger is charge arriving. Give up if
        # it does not, or if the terminal reaches the cell floor - we are here
        # precisely because the pack was already low, so there is no margin to
        # spend on optimism about VBus.
        millivolts = read_int(os.path.join(XPI, "battery"))
        milliamps = read_int(os.path.join(XPI, "amps"))
        if millivolts is not None and millivolts <= LOW_BATT_MV:
            log("charge-hold: %dmV, at the cell floor - powering off"
                % millivolts)
            return "poweroff"
        if milliamps is not None and milliamps < 0:
            if draining_since is None:
                draining_since = now
            elif now - draining_since >= CHARGE_HOLD_GIVEUP_S:
                log("charge-hold: still draining (%dmA) after %.0fs with the"
                    " screen off - VBus is lying, powering off"
                    % (milliamps, CHARGE_HOLD_GIVEUP_S))
                return "poweroff"
        else:
            draining_since = None

        publish_battery()  # keeps the gauge live and lets Full latch 100%
        if now >= next_save:
            next_save = now + 60.0
            save_battery_state()
        if BATT_LOG and now >= next_log:
            next_log = now + BATT_LOG_S
            mv = read_int(os.path.join(XPI, "battery"))
            ma = read_int(os.path.join(XPI, "amps"))
            if mv is not None and ma is not None:
                log_battery_sample(mv, ma, read_int(THERMAL))
        if now >= next_fan:  # ES is gone; keep the thermal safety net alive
            next_fan = now + FAN_UPDATE_S
            raw = read_int(THERMAL)
            if raw is not None:
                temp = raw / 1000.0 if raw > 1000 else float(raw)
                fan_band, duty = target_duty(FAN_PROFILES["quiet"][1],
                                             FAN_PROFILES["quiet"][0],
                                             temp, fan_band)
                if read_int(os.path.join(XPI, "fan")) != duty:
                    write_str(os.path.join(XPI, "fan"), duty)
        time.sleep(1.0)
    write_str(os.path.join(XPI, "flags"), "1")  # leave the console usable
    return "exit"


# ------------------------------------------------------- user messages ----
def retroarch_running():
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/comm" % pid) as f:
                if f.read().strip() == "retroarch":
                    return True
        except OSError:
            pass
    return False


def ra_command(command):
    """Sends a network command to RetroArch (needs network_cmd_enable=true)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(command.encode("utf-8"), RA_CMD_ADDR)
        s.close()
        return True
    except OSError:
        return False


def osd_enabled():
    try:
        with open(OSD_CONF) as f:
            for line in f:
                m = re.match(r"\s*enabled\s*=\s*(\d)", line)
                if m:
                    return m.group(1) != "0"
    except OSError:
        return False
    return True


def notify(text, seconds=6, warn=False):
    """On-screen message: through the OSD bar when it runs, else through
    RetroArch (in game) or EmulationStation (in the menus)."""
    log("message: %s" % text)
    if osd_enabled():
        try:
            tmp = OSD_MSG + ".tmp"
            with open(tmp, "w") as f:
                f.write("%d\n%s\n%s\n" % (int(time.time() + seconds),
                                           "warn" if warn else "info", text))
            os.replace(tmp, OSD_MSG)
            return
        except OSError:
            pass
    if retroarch_running():
        ra_command("SHOW_MSG " + text)
        return
    try:
        urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:1234/notify", data=text.encode("utf-8"),
            method="POST"), timeout=2).read()
    except Exception:
        pass


def newest_state_mtime():
    newest = 0.0
    for root, _dirs, files in os.walk(SAVES_DIR):
        for name in files:
            if ".state" in name and not name.endswith(".png"):
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(root, name)))
                except OSError:
                    pass
    return newest


def save_running_game():
    """Asks RetroArch for a save state before the game gets killed.

    Batocera numbers save states automatically (savestate_auto_index), so this
    adds a new state instead of overwriting one; EmulationStation offers it
    the next time the game is launched."""
    if not retroarch_running():
        return
    before = newest_state_mtime()
    if not ra_command("SAVE_STATE"):
        return
    deadline = time.monotonic() + SAVE_WAIT_S
    while time.monotonic() < deadline:
        time.sleep(0.5)
        if newest_state_mtime() > before:
            log("game state saved")
            notify("Game saved", 3)
            time.sleep(1.5)
            return
    log("no save state written (is global.retroarch.network_cmd_enable=true?)")


def shutdown(reason):
    """Stop ES first so game saves flush, then let the MCU cut power.

    Writing 0 to `flags` tells the MCU it may drop the rail, so it has to be
    the last thing before poweroff. Deliberately NOT done on reboot: the MCU
    would cut power instead of letting the board come back up.

    If the charger is attached AND the slider is still on - i.e. the low
    battery path, not the switch path - charge instead of powering off, which
    is far faster than charging switched off (see charge_hold).
    """
    global _running
    log("shutdown: %s" % reason)
    notify("Battery empty: saving" if reason.startswith("battery")
           else "Shutting down...", 10, warn=reason.startswith("battery"))
    save_running_game()
    action = "poweroff"
    status = read_int(os.path.join(XPI, "status"))
    switch_on = bool(status is not None and status & 0x40)
    if vbus_present():
        if switch_on:
            log("charge-hold: %s with charger attached - staying up to charge"
                % reason)
            action = charge_hold()
        else:
            # It will still charge switched off - measured overnight on
            # 08-07/08, a taper that ran 500mA -> 100mA with the console off -
            # but much more slowly than with the Pi resident. Worth saying so
            # the user knows to leave it on when they want it full quickly.
            log("note: powering off on the charger - it will still charge, but"
                " slowly; leave it switched on to charge at ~1A")
    _running = False
    save_battery_state()
    if action == "exit":
        return  # daemon stopped from outside; not ours to halt the system
    subprocess.run(["/etc/init.d/S31emulationstation", "stop"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["sync"])
    write_str(os.path.join(XPI, "flags"), "0")
    subprocess.run(["poweroff"])
    time.sleep(30)


# ------------------------------------------------------------------ main ----
def main():
    def on_signal(signum, _frame):
        global _running
        _running = False
        save_battery_state()
        log("caught signal %d, exiting" % signum)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    if not os.path.isdir(XPI):
        log("FATAL: %s missing - is xpi_gamecon loaded?" % XPI)
        return 1

    fan_profile, fan_idle, fan_curve = load_fan_config()
    fan_conf_mtime = _mtime(FAN_CONF_PATH)
    led_mode, led_red, led_green = load_led_config()
    led_conf_mtime = _mtime(LED_CONF_PATH)
    apply_led(led_red, led_green)
    bat_percent, bat_charging = None, False
    have_battery = power_supply_ready()
    fan_node = os.path.join(XPI, "fan")
    status_node = os.path.join(XPI, "status")
    volume_node = os.path.join(XPI, "volume")

    fan_band = 0
    last_volume = None
    switch_held_since = None
    next_battery_publish = 0.0
    next_fan_update = 0.0
    next_battery_log = 0.0
    next_state_save = 0.0
    warned = set()

    log("started (fan profile=%s idle=%d curve=%s, battery=%s, led=%s %d/%d)"
        % (fan_profile, fan_idle, fan_curve, "yes" if have_battery else "no",
           led_mode, led_red, led_green))

    while _running:
        now = time.monotonic()

        # --- power switch: bit 0x40 clear means the slider is off ---
        status = read_int(status_node)
        if status is not None:
            if not status & 0x40:
                if switch_held_since is None:
                    switch_held_since = now
                elif now - switch_held_since >= SWITCH_DEBOUNCE_S:
                    shutdown("power switch")
                    break
            else:
                switch_held_since = None

        # --- battery ---
        if have_battery and now >= next_battery_publish:
            next_battery_publish = now + BATT_PUBLISH_S
            percent, charging = publish_battery()
            bat_percent, bat_charging = percent, charging
            if percent is not None:
                if charging or percent > max(LOW_BATT_WARN_PCT) + 2:
                    warned.clear()
                elif any(percent <= lvl and lvl not in warned for lvl in LOW_BATT_WARN_PCT):
                    warned.update(lvl for lvl in LOW_BATT_WARN_PCT if percent <= lvl)
                    notify("Battery low: %d%%" % percent, 8, warn=True)
            if percent is not None and not charging:
                millivolts = read_int(os.path.join(XPI, "battery"))
                if percent <= LOW_BATT_PCT:
                    shutdown("battery %d%%" % percent)
                    break
                # Backstop on the raw terminal voltage: the percentage above is
                # inferred, this is measured, and the cell must not be dragged
                # down regardless of what the estimate says.
                if millivolts is not None and millivolts < LOW_BATT_MV:
                    shutdown("battery terminal %dmV" % millivolts)
                    break

        # --- persist the coulomb count for the next boot's seed ---
        if now >= next_state_save:
            next_state_save = now + BATT_STATE_SAVE_S
            save_battery_state()

        # --- raw sample log (calibration) ---
        if BATT_LOG and now >= next_battery_log:
            next_battery_log = now + BATT_LOG_S
            mv = read_int(os.path.join(XPI, "battery"))
            ma = read_int(os.path.join(XPI, "amps"))
            if mv is not None and ma is not None:
                log_battery_sample(mv, ma, read_int(THERMAL))

        # --- volume wheel ---
        volume = read_int(volume_node)
        if volume is not None:
            volume = max(0, min(100, volume))
            if last_volume is None or abs(volume - last_volume) >= 2:
                pin_builtin_audio_sink()
                subprocess.run(["batocera-audio", "setSystemVolume", str(volume)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                last_volume = volume

        # --- fan (and the LED, which rides the same slow tick) ---
        if now >= next_fan_update:
            next_fan_update = now + FAN_UPDATE_S
            m = _mtime(LED_CONF_PATH)
            if m != led_conf_mtime:
                led_conf_mtime = m
                led_mode, led_red, led_green = load_led_config()
                log("led reloaded: mode=%s red=%d green=%d"
                    % (led_mode, led_red, led_green))
            # Live-reload the profile if the user edited the config file.
            m = _mtime(FAN_CONF_PATH)
            if m != fan_conf_mtime:
                fan_conf_mtime = m
                new_profile, fan_idle, fan_curve = load_fan_config()
                fan_band = 0
                log("fan profile reloaded: %s idle=%d curve=%s"
                    % (new_profile, fan_idle, fan_curve))
            raw = read_int(THERMAL)
            if raw is not None:
                temp = raw / 1000.0 if raw > 1000 else float(raw)
                fan_band, duty = target_duty(fan_curve, fan_idle, temp, fan_band)
                # Compare against the node itself rather than a remembered
                # value: anything else on the system may have written to it,
                # and this way we converge back instead of latching.
                if read_int(fan_node) != duty:
                    write_str(fan_node, duty)

        # --- LED ---
        # In "battery" mode the colour follows the level (and pulses while
        # charging), so it is re-evaluated every loop; in "static" mode apply_led
        # only writes when the value changed, so this is nearly free.
        if led_mode == "battery" and bat_percent is not None:
            apply_led(*battery_led(bat_percent, bat_charging, now))
        else:
            apply_led(led_red, led_green)

        time.sleep(POLL_S)

    return 0


if __name__ == "__main__":
    sys.exit(main())
