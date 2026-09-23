#!/bin/bash
# Starts the full-screen System Settings menu. EmulationStation is frozen while
# the menu is shown (like a launched game) and the OSD is pushed aside (same
# "overlay" layer, they would overlap), then everything is put back.
#
# The OSD is supervised: killing it is not enough, it would come back after
# 3 s. So a pause file is created that the supervisor honours, and removed on
# exit - even if the menu crashes, thanks to the trap.
export XDG_RUNTIME_DIR=/var/run

# wayland-1 under sway (Pi 4 image), wayland-0 under labwc (Pi 3 image).
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

PAUSE=/tmp/piboy-osd.pause
LOG=/userdata/system/piboy-osd/settings.log
exec >>"$LOG" 2>&1
echo "=== $(date '+%F %T') settings (WAYLAND_DISPLAY=$WAYLAND_DISPLAY) ==="

ES=$(pidof emulationstation 2>/dev/null | awk '{print $1}')

cleanup() {
	[ -n "$ES" ] && kill -CONT "$ES" 2>/dev/null
	rm -f "$PAUSE"
	echo "$(date '+%F %T') exit, OSD handed back to its supervisor"
}
trap cleanup EXIT INT TERM HUP

touch "$PAUSE"
killall piboy-osd 2>/dev/null
[ -n "$ES" ] && kill -STOP "$ES"

/userdata/system/piboy-osd/piboy-settings
