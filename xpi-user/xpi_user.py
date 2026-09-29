#!/usr/bin/env python3
"""xpi_user.py - user-space driver for the Experimental Pi PiBoy controller MCU.

Talks to the MCU over GPIO 26 (clock) and GPIO 27 (data), bit for bit like
Experimental Pi's xpi_gamecon kernel module (GPL, Nathan Scherdin), but as a
normal program: no kernel module, so nothing to rebuild when Batocera updates
its kernel. One file for both consoles:

    --model xrs   PiBoy XRS, 14-byte frame, two sticks  (default)
    --model dmg   PiBoy DMG, 12-byte frame, one stick

Modes
    --test        decode the MCU frames and print them live until Ctrl-C
    --daemon      the driver itself (started by /boot/boot-custom.sh)
    --enable      install the boot hook and start the driver at every boot
    --disable     remove the boot hook
    --probe       tell a DMG from an XRS by the frames the MCU sends; prints
                  "dmg", "xrs" or "none" (used by the PiBoy layer's install.sh)

What the daemon does
  * polls the MCU 100 times a second. The MCU also takes this as the Pi's
    heartbeat: when it stops, the MCU cuts the board's power after a delay.
    Stopping the driver therefore sends flags=129 first (60 s grace, what the
    vendor image does on reboot), unless the system is powering off (flags=0);
  * creates the gamepad through /dev/uinput, same name and ids as the kernel
    driver;
  * mirrors the kernel sysfs files in /run/xpi_gamecon: version status battery
    amps percent volume (read), flags fan red green (read/write, applied
    within 0.1 s), plus raw (the last valid frame in hex, for diagnosis);
  * volume wheel -> batocera-audio, fan from the CPU temperature, power switch
    and empty battery -> clean shutdown. The PiBoy layer's daemon
    (piboy-dmgcontrol.py) takes these four over when it runs: it writes its
    pid in /run/xpi_gamecon/managed, and the driver steps back while that
    process lives.

Raspberry Pi 3 and 4 only: direct access to the BCM283x/BCM2711 GPIO registers,
like the kernel module (the Pi 5 moved its GPIOs into the RP1 chip).

Status: written 2026-09-25 from the vendor driver sources; run on one PiBoy XRS
(MCU firmware 1.0.7) on 2026-09-28: controls, menu button, shutdown OK. Not
yet run on a DMG. Run --test first.
"""
import argparse
import gc
import mmap
import os
import signal
import struct
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
RUN = "/run/xpi_gamecon"
BOOT_HOOK = "/boot/boot-custom.sh"
HOOK_MARK = "xpi-user boot hook"
LAYER_MARK = "PiBoy layer boot hook"    # the full layer's hook also starts this driver
THERMAL = "/sys/class/thermal/thermal_zone0/temp"

CLK_PIN, DAT_PIN = 26, 27
CLK, DAT = 1 << CLK_PIN, 1 << DAT_PIN
HALF_NS = 7000                      # kernel: udelay(7) per clock half-period
REG_FSEL2, REG_SET, REG_CLR, REG_LEV = 2, 7, 10, 13    # 32-bit word indexes
DAT_SHIFT = (DAT_PIN % 10) * 3      # function bits of GPIO 27 in GPFSEL2
CLK_SHIFT = (CLK_PIN % 10) * 3

SWITCH_DEBOUNCE_S = 1.5             # power switch must read "off" this long
LOW_MV, LOW_S = 3250, 60            # discharging below this for this long -> off
FAN_CURVE = [(60, 80), (67, 120), (73, 175), (79, 235)]   # "quiet" profile, 0-255
# /run/xpi_gamecon/fan is always 0-255, like the DMG kernel driver's sysfs node.
# It is scaled on the wire: the XRS MCU takes 0-100 (Hancock33's
# fan.piboyxrs.ini), so sending 80 there meant 80 %, and 120+ meant full speed.
FAN_CRITICAL_C, FAN_HYST_C = 80, 2.0

EXIT_NO_FRAMES, EXIT_CONFLICT = 3, 4    # the boot hook stops retrying on these


def modprobe(name):
    try:
        subprocess.run(["modprobe", name], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    except OSError:
        pass


def log(msg):
    sys.stderr.write(time.strftime("%H:%M:%S ") + msg + "\n")
    sys.stderr.flush()


# ------------------------------------------------------------------ CRC16 ---
def _table():
    t = []
    for i in range(256):
        c = i << 8
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) if c & 0x8000 else (c << 1)
        t.append(c & 0xFFFF)
    return t


CRC_TABLE = _table()


def crc16(data):
    """CRC-16/CCITT, poly 0x1021, init 0, MSB first - same as the kernel driver."""
    c = 0
    for b in data:
        c = ((c << 8) & 0xFFFF) ^ CRC_TABLE[((c >> 8) ^ b) & 0xFF]
    return c


# ----------------------------------------------------------------- models ---
def _sb(v):
    return v - 256 if v > 127 else v


def decode_xrs(b):
    """14 bytes: version, LX, LY, RX, RY, pad, buttons, status, volume,
    battery, current, percent, CRC lo, CRC hi (from the vendor XRS driver)."""
    p, q = b[5], b[6]
    keys = {
        "BTN_A": not p & 0x10, "BTN_B": not p & 0x20,
        "BTN_X": not p & 0x40, "BTN_Y": not p & 0x80,
        "BTN_TL": not q & 0x01, "BTN_TR": not q & 0x02,
        "BTN_TL2": not q & 0x04, "BTN_TR2": not q & 0x08,
        "BTN_THUMBL": not q & 0x10, "BTN_THUMBR": not q & 0x20,
        "BTN_SELECT": not q & 0x40, "BTN_START": not q & 0x80,
    }
    axes = {
        "ABS_X": b[1], "ABS_Y": b[2], "ABS_RX": b[3], "ABS_RY": b[4],
        "ABS_HAT0X": int(not p & 0x08) - int(not p & 0x04),
        "ABS_HAT0Y": int(not p & 0x02) - int(not p & 0x01),
    }
    return {
        "version": ((b[0] & 0xC0) << 2) | (b[0] & 0x3F),
        "keys": keys, "axes": axes,
        "status": b[7], "volume": b[8],
        "battery": b[9] * 5 + 2950, "amps": _sb(b[10]) * 50, "percent": b[11],
    }


def decode_dmg(b):
    """12 bytes, as in the vendor DMG driver (firmware 1.02 and later)."""
    k, d = b[3], b[4]
    keys = {
        "BTN_A": not k & 0x01, "BTN_B": not k & 0x02, "BTN_C": not k & 0x04,
        "BTN_X": not k & 0x08, "BTN_Y": not k & 0x10, "BTN_Z": not k & 0x20,
        "BTN_SELECT": bool(k & 0x40), "BTN_START": bool(k & 0x80),
        "BTN_THUMBL": bool(d & 0x40),
        "BTN_DPAD_UP": bool(d & 0x01), "BTN_DPAD_DOWN": bool(d & 0x02),
        "BTN_DPAD_LEFT": bool(d & 0x04), "BTN_DPAD_RIGHT": bool(d & 0x08),
        "BTN_TL": bool(d & 0x10), "BTN_TR": bool(d & 0x20),
    }
    return {
        "version": ((b[0] & 0xC0) << 2) | (b[0] & 0x3F),
        "keys": keys, "axes": {"ABS_X": b[1], "ABS_Y": b[2]},
        "status": b[5] & 0xC6, "volume": b[6],
        "battery": b[7] * 5 + 2950, "amps": _sb(b[8]) * 50, "percent": b[9],
    }


MODELS = {
    "xrs": {"length": 14, "decode": decode_xrs, "name": "Experimental Pi Controller",
            "fan_max": 100,
            "axes": {"ABS_X": (0, 255), "ABS_Y": (0, 255), "ABS_RX": (0, 255),
                     "ABS_RY": (0, 255), "ABS_HAT0X": (-1, 1), "ABS_HAT0Y": (-1, 1)}},
    "dmg": {"length": 12, "decode": decode_dmg, "name": "PiBoy DMG Controller",
            "fan_max": 255,
            "axes": {"ABS_X": (0, 255), "ABS_Y": (0, 255)}},
}


# ------------------------------------------------------------------- GPIO ---
def _peripheral_base():
    """Same lookup as the kernel module: /soc 'ranges', cell 1, else cell 2."""
    raw = open("/proc/device-tree/soc/ranges", "rb").read()
    cells = struct.unpack(">%dI" % (len(raw) // 4), raw[:len(raw) // 4 * 4])
    return cells[1] if cells[1] else cells[2]


class Gpio:
    def __init__(self):
        self.mm, self.how = self._map()
        self.r = memoryview(self.mm).cast("I")
        r = self.r
        r[REG_CLR] = CLK
        r[REG_FSEL2] = (r[REG_FSEL2] & ~(7 << CLK_SHIFT)) | (1 << CLK_SHIFT)   # clock: output
        r[REG_FSEL2] = r[REG_FSEL2] & ~(7 << DAT_SHIFT)                        # data: input

    @staticmethod
    def _map():
        devs = ["/dev/gpiomem", "/dev/gpiomem0"]
        if not any(os.path.exists(d) for d in devs):
            modprobe("raspberrypi-gpiomem")
            time.sleep(0.2)
        for d in devs:
            try:
                fd = os.open(d, os.O_RDWR | os.O_SYNC)
            except OSError:
                continue
            try:
                return mmap.mmap(fd, 4096, mmap.MAP_SHARED,
                                 mmap.PROT_READ | mmap.PROT_WRITE, offset=0), d
            except OSError:
                pass
            finally:
                os.close(fd)
        base = _peripheral_base() + 0x200000
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        try:
            return mmap.mmap(fd, 4096, mmap.MAP_SHARED,
                             mmap.PROT_READ | mmap.PROT_WRITE, offset=base), \
                "/dev/mem @0x%08x" % base
        finally:
            os.close(fd)

    def transfer(self, length, reply):
        """One exchange, exactly like gc_timer(): read `length` bytes, one
        turnaround clock, then send the 4 reply bytes if the frame's CRC is
        good. Returns (frame, crc_ok)."""
        r = self.r
        pc = time.perf_counter_ns
        H = HALF_NS
        r[REG_FSEL2] = r[REG_FSEL2] & ~(7 << DAT_SHIFT)            # data: input
        buf = bytearray(length)
        for i in range(length):
            v = 0
            for _ in range(8):
                r[REG_SET] = CLK
                t = pc() + H
                while pc() < t:
                    pass
                r[REG_CLR] = CLK
                t = pc() + H
                while pc() < t:
                    pass
                v = (v << 1) | (1 if r[REG_LEV] & DAT else 0)
            buf[i] = v
        r[REG_FSEL2] = (r[REG_FSEL2] & ~(7 << DAT_SHIFT)) | (1 << DAT_SHIFT)   # output
        r[REG_SET] = CLK
        t = pc() + H
        while pc() < t:
            pass
        r[REG_CLR] = CLK
        t = pc() + H
        while pc() < t:
            pass
        ok = bool(buf[0]) and crc16(buf[:-2]) == (buf[-1] << 8) | buf[-2]
        if ok:
            for byte in reply:
                for bit in range(7, -1, -1):
                    r[REG_SET if (byte >> bit) & 1 else REG_CLR] = DAT
                    r[REG_SET] = CLK
                    t = pc() + H
                    while pc() < t:
                        pass
                    r[REG_CLR] = CLK
                    t = pc() + H
                    while pc() < t:
                        pass
        r[REG_FSEL2] = r[REG_FSEL2] & ~(7 << DAT_SHIFT)            # data: input
        return bytes(buf), ok


# ------------------------------------------------------------ run-time state
SLOTS = ("flags", "fan", "red", "green")        # order of the kernel's values.data[]
READ_FILES = ("version", "status", "battery", "amps", "percent", "volume")


def _write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _read_int(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


class Driver:
    def __init__(self, args):
        self.args = args
        self.model = MODELS[args.model]
        self.gpio = Gpio()
        self.values = {"flags": 1, "fan": 80, "red": 2, "green": 2}
        self.dirty = list(SLOTS)
        self.index = 0
        self.own_mtime = {}
        self.state = None
        self.raw = None
        self.ok = self.err = 0
        self.t_frame = []
        self.stop = False
        self.ui = None
        self.fan_band = 0
        self.last_volume = None
        self.volume_proc = None
        self.switch_seen_on = False
        self.switch_off_since = None
        self.low_since = None
        self.shutdown = None                    # (stage, t0, proc)

    # -- control/state files --------------------------------------------------
    def setup_run(self):
        os.makedirs(RUN, exist_ok=True)
        for name in SLOTS:
            self.set_value(name, self.values[name])

    def set_value(self, name, v, write_file=True):
        v = max(0, min(255, int(v)))
        if self.values.get(name) != v:
            self.values[name] = v
            if name not in self.dirty:
                self.dirty.append(name)
        if write_file:
            p = os.path.join(RUN, name)
            _write(p, "%d" % v)
            self.own_mtime[name] = os.stat(p).st_mtime_ns

    def poll_controls(self):
        for name in SLOTS:
            p = os.path.join(RUN, name)
            try:
                m = os.stat(p).st_mtime_ns
            except OSError:
                continue
            if m != self.own_mtime.get(name):
                self.own_mtime[name] = m
                v = _read_int(p)
                if v is not None:
                    self.set_value(name, v, write_file=False)

    def managed(self):
        """The PiBoy layer's daemon runs the fan, volume and power policies,
        or a shutdown is already under way: leave them alone."""
        if os.path.exists(os.path.join(RUN, "poweroff")):
            return True
        pid = _read_int(os.path.join(RUN, "managed"))
        return pid is not None and os.path.exists("/proc/%d" % pid)

    def publish(self):
        s = self.state
        if s:
            for name in READ_FILES:
                _write(os.path.join(RUN, name), "%d" % s[name])
            _write(os.path.join(RUN, "raw"), self.raw.hex(" "))

    # -- one frame ----------------------------------------------------------------
    def next_reply(self):
        name = self.dirty[0] if self.dirty else SLOTS[self.index & 3]
        idx = SLOTS.index(name)
        v = self.values[name]
        if name == "fan":
            v = (v * self.model["fan_max"] + 127) // 255
        pay = bytes([0xC0 | idx, v & 0xFF])
        c = crc16(pay)
        return name, pay + bytes([c >> 8, c & 0xFF])

    def frame(self):
        name, reply = self.next_reply()
        t0 = time.perf_counter_ns()
        buf, ok = self.gpio.transfer(self.model["length"], reply)
        self.t_frame.append(time.perf_counter_ns() - t0)
        if not ok:
            self.err += 1
            return None
        self.ok += 1
        if name in self.dirty:
            self.dirty.remove(name)
        else:
            self.index += 1
        if self.args.model == "dmg" and buf[0] in (0xA5, 0x5A):
            raise SystemExit("DMG firmware 1.00/1.01 speaks an older reply format: "
                             "update the MCU to 1.06 first")
        self.raw = buf
        self.state = self.model["decode"](buf)
        return self.state

    # -- gamepad ------------------------------------------------------------------
    def open_uinput(self):
        from evdev import UInput, AbsInfo, ecodes as e
        if not os.path.exists("/dev/uinput"):
            modprobe("uinput")
        keys = sorted(getattr(e, k) for k in self.model["decode"](bytes(16))["keys"])
        absinfo = []
        for name, (lo, hi) in self.model["axes"].items():
            mid = 0 if lo < 0 else (lo + hi) // 2
            absinfo.append((getattr(e, name), AbsInfo(mid, lo, hi, 0, 0, 0)))
        self.e = e
        self.ui = UInput({e.EV_KEY: keys, e.EV_ABS: absinfo}, name=self.model["name"],
                         bustype=0x15, vendor=0x0001, product=0x0001, version=0x0100)
        self.last_keys, self.last_axes = {}, {}

    def emit(self, s):
        e, ui, changed = self.e, self.ui, False
        for k, v in s["keys"].items():
            v = 1 if v else 0
            if self.last_keys.get(k) != v:
                ui.write(e.EV_KEY, getattr(e, k), v)
                self.last_keys[k] = v
                changed = True
        for a, v in s["axes"].items():
            if self.last_axes.get(a) != v:
                ui.write(e.EV_ABS, getattr(e, a), v)
                self.last_axes[a] = v
                changed = True
        if changed:
            ui.syn()

    # -- slow housekeeping --------------------------------------------------------
    def fan_tick(self):
        t = _read_int(THERMAL)
        if t is None:
            return
        c = t / 1000.0
        if c >= FAN_CRITICAL_C:
            self.set_value("fan", 255)
            return
        band = sum(1 for th, _ in FAN_CURVE if c >= th)
        if band < self.fan_band and c > FAN_CURVE[self.fan_band - 1][0] - FAN_HYST_C:
            band = self.fan_band
        self.fan_band = band
        self.set_value("fan", FAN_CURVE[band - 1][1] if band else 0)

    def volume_tick(self):
        s = self.state
        if self.volume_proc:
            rc = self.volume_proc.poll()
            if rc is None:
                return
            if rc != 0:                 # audio not up yet (early boot): retry
                self.last_volume = None
            self.volume_proc = None
        if not s:
            return
        v = max(0, min(100, s["volume"]))
        if self.last_volume is None or abs(v - self.last_volume) >= 2:
            self.volume_proc = subprocess.Popen(
                ["batocera-audio", "setSystemVolume", str(v)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.last_volume = v

    def power_tick(self, now):
        s = self.state
        if not s or self.shutdown:
            return
        if s["status"] & 0x40:
            self.switch_seen_on = True
            self.switch_off_since = None
        elif self.switch_seen_on:            # never act on a bit never seen set
            if self.switch_off_since is None:
                self.switch_off_since = now
            elif now - self.switch_off_since >= SWITCH_DEBOUNCE_S:
                self.begin_shutdown("power switch")
                return
        if s["amps"] < 0 and s["battery"] < LOW_MV:
            if self.low_since is None:
                self.low_since = now
            elif now - self.low_since >= LOW_S:
                self.begin_shutdown("battery %d mV" % s["battery"])
        else:
            self.low_since = None

    def begin_shutdown(self, reason):
        """Keep polling (it is the heartbeat) while ES stops and disks sync,
        then flags=0 and poweroff; the boot hook sees our marker and keeps 0."""
        log("shutdown: %s" % reason)
        open(os.path.join(RUN, "poweroff"), "w").close()
        proc = subprocess.Popen(["/etc/init.d/S31emulationstation", "stop"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.shutdown = ["es", time.monotonic(), proc]

    def shutdown_tick(self, now):
        stage, t0, proc = self.shutdown
        if stage in ("es", "sync") and (proc.poll() is not None or now - t0 > 30):
            if stage == "es":
                self.shutdown = ["sync", now, subprocess.Popen(["sync"])]
            else:
                self.set_value("flags", 0)
                self.shutdown = ["flags", now, None]
        elif stage == "flags" and now - t0 > 0.5:
            subprocess.Popen(["poweroff"])
            self.shutdown = ["poweroff", now, None]

    # -- main loop ------------------------------------------------------------------
    def realtime(self):
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(40))
        except (OSError, AttributeError) as exc:
            log("no real-time priority (%s)" % exc)

    def run(self, test=False):
        period = 1e9 / self.args.hz
        self.setup_run()
        if not test:
            self.open_uinput()
        self.realtime()
        gc.disable()
        start = time.monotonic()
        nxt = time.perf_counter_ns()
        # The fan policy waits 1 s, so the MCU first gets the non-zero start
        # value: a fan sent 0 from the very first frame stayed at full speed on
        # an XRS until another value came (the vendor drivers start at 10 too).
        slow = {"ctl": 0.0, "pub": 0.0, "fan": start + 1.0, "vol": 0.0, "gc": 0.0,
                "stat": 0.0, "own": 0.0}
        own = True
        last_print = None
        while not self.stop:
            s = self.frame()
            now = time.monotonic()
            if s and self.ui:
                self.emit(s)
            if test and s:
                line = describe(s)
                if line != last_print:
                    print(line, flush=True)
                    last_print = line
            if not test and self.ok == 0 and now - start > 5:
                log("no valid frame in 5 s (%d CRC errors): wrong --model, or the "
                    "timing does not suit this MCU; disabling myself" % self.err)
                return EXIT_NO_FRAMES
            if now >= slow["ctl"]:
                slow["ctl"] = now + 0.1
                self.poll_controls()
            if now >= slow["pub"]:
                slow["pub"] = now + 0.5
                self.publish()
            if now >= slow["own"]:
                slow["own"] = now + 1.0
                own = test or not self.managed()
            if own and now >= slow["fan"]:
                slow["fan"] = now + 2.0
                self.fan_tick()
            if own and not test and now >= slow["vol"]:
                slow["vol"] = now + 0.2
                self.volume_tick()
            if not test and not self.args.no_power:
                if self.shutdown:
                    self.shutdown_tick(now)
                elif own:
                    self.power_tick(now)
                else:                           # stay ready to take over
                    self.switch_off_since = self.low_since = None
                    if s and s["status"] & 0x40:
                        self.switch_seen_on = True
            if now >= slow["stat"]:
                slow["stat"] = now + (2.0 if test else 60.0)
                self.report(test)
            if now >= slow["gc"]:
                slow["gc"] = now + 10.0
                gc.collect()
            nxt += period
            wait = nxt - time.perf_counter_ns()
            if wait > 0:
                time.sleep(wait / 1e9)
            else:
                nxt = time.perf_counter_ns()
        return 0

    def report(self, test):
        n = self.ok + self.err
        ft = self.t_frame[-500:]
        self.t_frame = ft
        s = self.state or {}
        msg = ("frames ok %d, CRC errors %d (%.3f%%), frame time avg %.2f ms max %.2f ms"
               % (self.ok, self.err, 100.0 * self.err / n if n else 0,
                  sum(ft) / len(ft) / 1e6 if ft else 0, max(ft) / 1e6 if ft else 0))
        if s:
            msg += (" | fw %x status 0x%02x battery %d mV %d mA %d%% volume %d fan %d"
                    % (s["version"], s["status"], s["battery"], s["amps"],
                       s["percent"], s["volume"], self.values["fan"]))
        if test:
            print("   [" + msg + "]", flush=True)
        else:
            log(msg)

    def finish(self):
        """Last frames before exiting: never leave the MCU without a plan."""
        if self.values["flags"] != 0:
            if os.path.exists(os.path.join(RUN, "stop")):
                self.poll_controls()            # the boot hook wrote 0 or 129
            else:
                self.set_value("flags", 129)    # heartbeat about to stop: 60 s grace
        self.dirty = ["flags"]
        for _ in range(20):
            self.frame()
            if not self.dirty:
                break
            time.sleep(0.01)
        for _ in range(5):                      # repeat, in case one was lost
            self.dirty = ["flags"]
            self.frame()
            time.sleep(0.01)
        if self.ui:
            self.ui.close()
        return self.values["flags"]


def probe(frames=30):
    """Which frame length gets through the CRC: 12 bytes (DMG) or 14 (XRS).
    A wrong length never passes the CRC, so it is never answered either. The
    right one gets the driver's usual first reply (flags=1, display on)."""
    gpio = Gpio()
    flags_on = bytes([0xC0, 1])
    c = crc16(flags_on)
    reply = flags_on + bytes([c >> 8, c & 0xFF])
    good = {}
    for model in ("dmg", "xrs"):
        n = 0
        for _ in range(frames):
            _buf, ok = gpio.transfer(MODELS[model]["length"], reply)
            n += ok
            time.sleep(0.01)
        good[model] = n
    best = max(good, key=good.get)
    return best if good[best] >= frames // 3 else "none", good


def describe(s):
    pressed = [k[4:] for k, v in s["keys"].items() if v]
    ax = s["axes"]
    sticks = " ".join("%s=%d" % (a[4:], v if a.startswith("ABS_HAT") else v // 16 * 16)
                      for a, v in ax.items())
    sw = "on" if s["status"] & 0x40 else "OFF"
    return ("buttons: %-30s sticks: %s  switch: %s"
            % (" ".join(pressed) or "-", sticks, sw))


# --------------------------------------------------------------- checks ---
def preflight():
    if os.geteuid() != 0:
        raise SystemExit("run as root")
    try:
        compat = open("/proc/device-tree/compatible", "rb").read()
    except OSError:
        compat = b""
    if b"bcm2712" in compat:
        raise SystemExit("Raspberry Pi 5: its GPIOs are in the RP1 chip, not supported")
    for mod in ("xpi_gamecon", "xpi_gamecon_xrs"):
        if os.path.isdir("/sys/module/" + mod):
            log("the %s kernel module is loaded and owns GPIO 26/27: "
                "run 'rmmod %s' (and do not load it at boot)" % (mod, mod))
            return EXIT_CONFLICT
    return 0


def already_running():
    pid = _read_int(os.path.join(RUN, "pid"))
    return pid is not None and pid != os.getpid() and os.path.exists("/proc/%d" % pid)


# ---------------------------------------------------------- boot hook ---
def remount_boot(mode):
    subprocess.run(["mount", "-o", "remount," + mode, "/boot"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def enable(model):
    if HERE != "/boot/xpi-user":
        raise SystemExit("copy this folder to /boot/xpi-user first (the boot hook runs it "
                         "from there, before /userdata is mounted)")
    ours = os.path.join(HERE, "boot-custom.sh")
    current = open(BOOT_HOOK).read() if os.path.exists(BOOT_HOOK) else ""
    layer = LAYER_MARK in current
    if current and not layer and HOOK_MARK not in current:
        raise SystemExit(
            "%s already exists and is not ours. Merge the start/stop sections of\n"
            "%s into it by hand, then: touch %s/enabled" % (BOOT_HOOK, ours, HERE))
    remount_boot("rw")
    try:
        if not layer:
            with open(ours) as f, open(BOOT_HOOK + ".tmp", "w") as g:
                g.write(f.read())
            os.chmod(BOOT_HOOK + ".tmp", 0o755)
            os.replace(BOOT_HOOK + ".tmp", BOOT_HOOK)
        _write(os.path.join(HERE, "model"), model + "\n")
        open(os.path.join(HERE, "enabled"), "w").close()
        subprocess.run(["sync"])
    finally:
        remount_boot("ro")
    print("enabled (model %s): the driver starts at every boot%s. Reboot now."
          % (model, " (through the PiBoy layer's boot hook)" if layer else ""))


def disable(quiet=False):
    remount_boot("rw")
    try:
        for p in (os.path.join(HERE, "enabled"),):
            if os.path.exists(p):
                os.remove(p)
        if os.path.exists(BOOT_HOOK) and HOOK_MARK in open(BOOT_HOOK).read():
            os.remove(BOOT_HOOK)
        subprocess.run(["sync"])
    finally:
        remount_boot("ro")
    if not quiet:
        print("disabled: the driver no longer starts at boot.")


# ------------------------------------------------------------------ main ---
def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--test", action="store_true")
    g.add_argument("--daemon", action="store_true")
    g.add_argument("--enable", action="store_true")
    g.add_argument("--disable", action="store_true")
    g.add_argument("--probe", action="store_true")
    ap.add_argument("--model", choices=sorted(MODELS), default="xrs")
    ap.add_argument("--hz", type=float, default=100.0)
    ap.add_argument("--no-power", action="store_true",
                    help="leave the power switch and low battery to another program")
    args = ap.parse_args()

    if args.enable:
        return enable(args.model)
    if args.disable:
        return disable()

    rc = preflight()
    if rc:
        if args.daemon:
            disable(quiet=True)
        return rc
    if args.probe:
        if already_running():
            raise SystemExit("the driver is running: it already knows the model")
        model, good = probe()
        log("probe: %s (valid frames: dmg %d, xrs %d)" % (model, good["dmg"], good["xrs"]))
        print(model)
        return 0 if model != "none" else 1
    os.makedirs(RUN, exist_ok=True)
    if already_running():
        raise SystemExit("another xpi_user.py is running (pid in %s/pid)" % RUN)
    _write(os.path.join(RUN, "pid"), "%d" % os.getpid())
    os.chdir("/")

    drv = Driver(args)
    log("model %s, %d-byte frame, GPIO through %s, %g Hz"
        % (args.model, drv.model["length"], drv.gpio.how, args.hz))

    def on_signal(_sig, _frm):
        drv.stop = True
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        rc = drv.run(test=args.test)
    finally:
        flags = drv.finish()        # even after a crash: never leave the MCU without a plan
    if rc == EXIT_NO_FRAMES:
        disable(quiet=True)
    if args.test:
        drv.report(True)
        if flags == 129:
            print("\nStopped. The MCU was told a restart is coming: it keeps the Pi powered\n"
                  "for about 60 s without the driver. Within that time, run 'poweroff'\n"
                  "(or start the driver again).")
    else:
        log("stopped, flags=%d" % flags)
    return rc


if __name__ == "__main__":
    sys.exit(main())
