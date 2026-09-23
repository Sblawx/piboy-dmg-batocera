#!/usr/bin/env python3
"""Watch the PiBoy pad for SELECT+START and stop m8c, the way a Batocera game exits.

m8c's own `gamepad_quit` is a single button (Z here) because its config has no
notion of a chord, so the familiar SELECT+START has to come from outside.

This process is also the safety net for the frozen EmulationStation: whatever
happens — combo pressed, m8c dying on its own, this watcher being killed — it
sends SIGCONT to ES on the way out. If it did not, a crash would leave the
console looking dead: ES stopped, nothing on screen, no input.

Usage:  m8c-quit-watch.py <m8c-pid> <es-pid>
"""
import os
import signal
import sys
import time

from evdev import InputDevice, categorize, ecodes  # noqa: F401  (categorize kept for debugging)

PAD = "/dev/input/event0"          # "PiBoy DMG Controller"
COMBO = (ecodes.BTN_SELECT, ecodes.BTN_START)   # 314, 315
POLL = 1.0


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def thaw(es_pid):
    if es_pid and alive(es_pid):
        try:
            os.kill(es_pid, signal.SIGCONT)
        except OSError:
            pass


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    m8c_pid, es_pid = int(sys.argv[1]), int(sys.argv[2])

    try:
        dev = InputDevice(PAD)
    except Exception as exc:
        print("cannot open %s: %s" % (PAD, exc))
        thaw(es_pid)
        return 1

    held = set()
    last_check = time.time()
    # Non-blocking reads so we can also poll whether m8c is still there.
    os.set_blocking(dev.fd, False)

    try:
        while True:
            try:
                for event in dev.read():
                    if event.type != ecodes.EV_KEY:
                        continue
                    if event.value == 1:
                        held.add(event.code)
                    elif event.value == 0:
                        held.discard(event.code)
                    if all(code in held for code in COMBO):
                        print("SELECT+START -> stopping m8c")
                        if alive(m8c_pid):
                            os.kill(m8c_pid, signal.SIGTERM)
                        return 0
            except BlockingIOError:
                pass
            except OSError:
                return 0

            now = time.time()
            if now - last_check >= POLL:
                last_check = now
                if not alive(m8c_pid):
                    return 0          # m8c exited by itself; nothing to do
            time.sleep(0.02)
    finally:
        thaw(es_pid)


if __name__ == "__main__":
    sys.exit(main())
