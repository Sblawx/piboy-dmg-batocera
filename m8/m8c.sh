#!/bin/bash
# Launcher for m8c (Dirtywave M8 headless client) on the PiBoy DMG / Batocera 43.
#
# m8c itself is a plain aarch64 binary linked against Batocera's own
# libSDL3.so.0 (3.2.18) and libserialport.so.0 — nothing is bundled. It was
# cross-built in an Ubuntu 24.04 arm64 chroot; see build.sh in the project folder.
#
# Everything below is the glue that Batocera does NOT provide for a "port":
#   - the Wayland/pipewire environment (Sway does not export it),
#   - the USB serial + USB audio modules for the Teensy,
#   - an SDL gamepad mapping, because the PiBoy pad is not in gamecontrollerdb,
#   - and making this behave like a launched game rather than a window that
#     happens to be on top: EmulationStation is frozen for the duration.

M8C_DIR=/userdata/system/m8c
LOG=$M8C_DIR/m8c.log

exec >>"$LOG" 2>&1
echo "=== $(date '+%F %T') launching m8c ==="

# --- Sway/pipewire environment -----------------------------------------------
# Sway leaves WAYLAND_DISPLAY empty in its own env and the socket lives in
# /var/run, not in a per-user XDG dir. Batocera's SDL3 is built WITHOUT the x11
# backend, so Xwayland is not an option here: we must be a native Wayland client.
export XDG_RUNTIME_DIR=/var/run
# --- Wayland socket ----------------------------------------------------------
# The socket name depends on the compositor: wayland-1 under sway (Pi 4 image),
# wayland-0 under labwc (Pi 3 image). Take the first socket actually present in
# XDG_RUNTIME_DIR, falling back to wayland-1.
pick_wayland_socket() {
	for s in "$XDG_RUNTIME_DIR"/wayland-[0-9]*; do
		case "$s" in *.lock) continue ;; esac
		[ -S "$s" ] || continue
		basename "$s"
		return 0
	done
	echo wayland-1
}
export WAYLAND_DISPLAY=$(pick_wayland_socket)
export SDL_VIDEODRIVER=wayland
# pipewire owns the card; going straight to ALSA fails with "Host is down".
export SDL_AUDIODRIVER=pipewire

# --- Gamepad ------------------------------------------------------------------
# "PiBoy DMG Controller" is an out-of-tree xpi_gamecon device, absent from
# gamecontrollerdb.txt, so SDL sees a bare joystick and m8c's gamepad bindings
# (which are expressed as SDL_GamepadButton values) do nothing. The button
# indices below come from es_input.cfg, which pairs each SDL index with its
# evdev code on this exact hardware.
#
#   A=b5(304)  B=b6(305)  C=b7(306)  X=b8(307)  Y=b9(308)  Z=b10(309)
#   L=b11(310) R=b12(311) SELECT=b13(314) START=b14(315) L3=b15(317)
#
# Resulting M8 layout:
#   D-pad -> arrows | A -> EDIT | B -> OPTION | SELECT -> SHIFT | START -> PLAY
#   Z -> quit (m8c gamepad_quit=8/RIGHT_STICK) | L -> reset (gamepad_reset=9)
#   SELECT+START also quits, via the watcher below.
export SDL_GAMECONTROLLERCONFIG="15000000010000000100000000010000,PiBoy DMG Controller,platform:Linux,a:b5,b:b6,x:b8,y:b9,back:b13,start:b14,leftshoulder:b11,rightshoulder:b12,rightstick:b10,guide:b7,dpup:h0.1,dpdown:h0.4,dpleft:h0.8,dpright:h0.2,leftx:a0,lefty:a1,"

# --- Force the real speaker/jack as the pipewire default sink ---------------
# The Teensy's own USB audio device ("M8 Analog Stereo") is a full ALSA sink as
# well as a source, and wireplumber's newest-device-wins policy grabs it as the
# DEFAULT SINK the moment it enumerates — silently rerouting both m8c's own
# playback stream AND EmulationStation's background music into the M8's USB
# audio output, which goes nowhere audible. m8c's capture side is unaffected
# (it searches for a device named "M8" explicitly), only playback drifts.
# Re-pin the built-in card every launch, since replugging the Teensy re-triggers
# the hijack. Resolve the id by name rather than hardcoding it: pipewire object
# ids are not stable across reboots.
# wpctl prefixes every line with a box-drawing character (│), which \s
# does not match, so anchoring the id to line-start silently failed here.
# Just take the first number on the matched line instead.
BUILTIN_SINK=$(wpctl status 2>/dev/null | grep -m1 'Built-in Audio Stereo' | grep -oE '[0-9]+' | head -1)
if [ -n "$BUILTIN_SINK" ]; then
  wpctl set-default "$BUILTIN_SINK" 2>/dev/null && echo "default sink pinned to Built-in Audio Stereo (id $BUILTIN_SINK)"
else
  echo "WARNING: could not resolve Built-in Audio Stereo id via wpctl — sound may route to the M8's own USB audio device instead of the speaker/jack"
fi

# --- Teensy: USB serial + USB audio ------------------------------------------
# Both are modules (not built in) on the 6.12.25-v8 kernel. udevd normally
# autoloads them from modalias when the Teensy is enumerated; modprobe here is
# belt-and-braces for the case where m8c is started before/without udev having
# seen the device.
/sbin/modprobe cdc-acm 2>/dev/null
/sbin/modprobe snd-usb-audio 2>/dev/null

# Give a freshly plugged Teensy a moment to enumerate. If it never shows up we
# still start: m8c draws its own "M8 DEVICE NOT DETECTED" screen and picks the
# device up when it appears, which is friendlier than dumping the user back to
# EmulationStation. SELECT+START gets them out.
for _ in $(seq 1 20); do
  [ -e /dev/ttyACM0 ] && break
  sleep 0.25
done
[ -e /dev/ttyACM0 ] && echo "serial device: $(ls -l /dev/ttyACM0)" \
                    || echo "WARNING: no /dev/ttyACM0 — starting anyway"

# --- Behave like a launched game ---------------------------------------------
# EmulationStation keeps running behind a port: its background music keeps
# playing, and after ScreenSaverTime (60 s here) its screensaver hook writes
# flags=0 and cuts the LCD rail in the middle of a session. Freezing ES stops
# both, costs nothing, and is reversible — unlike stopping the service, which
# can tear down sway. Also saves ~108 mA of ES rendering.
ES_PID=$(pidof emulationstation 2>/dev/null | awk '{print $1}')

thaw_es() {
  [ -n "$ES_PID" ] && kill -CONT "$ES_PID" 2>/dev/null
}
cleanup() {
  [ -n "${WATCH_PID:-}" ] && kill "$WATCH_PID" 2>/dev/null
  thaw_es
  # If the screensaver blanked the panel just before we froze ES, make sure the
  # user does not get a black screen back.
  echo 1 > /sys/kernel/xpi_gamecon/flags 2>/dev/null
}
trap cleanup EXIT INT TERM HUP

# The panel may already be off if the screensaver fired while we were starting.
# Both halves matter: flags=1 restores the LCD rail, and sway must re-enable the
# output it powered off — otherwise SDL sees no display and m8c dies with
# "The video driver did not add any displays" (hit for real on 43.1).
echo 1 > /sys/kernel/xpi_gamecon/flags 2>/dev/null
SWAYSOCK=/var/run/sway-ipc.0.sock swaymsg 'output * power on' >/dev/null 2>&1

if [ -n "$ES_PID" ]; then
  kill -STOP "$ES_PID" 2>/dev/null && echo "EmulationStation ($ES_PID) frozen"
fi

cd "$M8C_DIR" || exit 1
# Optional launch preset: a caller (e.g. an "M8 (low latency)" port) can export
# M8C_CONFIG=/path/to/config.ini to run m8c against a specific config, without
# disturbing the default one. m8c both reads AND writes that path, so each
# preset keeps its own persisted state. Unset -> m8c uses its own default
# (~/.local/share/m8c/config.ini) exactly as before.
M8C_ARGS=()
if [ -n "${M8C_CONFIG:-}" ]; then
  M8C_ARGS+=(--config "$M8C_CONFIG")
  echo "using config preset: $M8C_CONFIG"
fi
./m8c "${M8C_ARGS[@]}" &
M8C_PID=$!

# SELECT+START -> quit, plus a safety net that thaws ES if anything goes wrong.
python3 "$M8C_DIR/m8c-quit-watch.py" "$M8C_PID" "${ES_PID:-0}" &
WATCH_PID=$!

wait "$M8C_PID"
status=$?
echo "=== m8c exited with status $status ==="
exit $status
