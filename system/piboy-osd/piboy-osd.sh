#!/bin/bash
# Supervisor for the piboy-osd overlay on Batocera (sway on the Pi 4 image,
# labwc on the Pi 3 image: both wlroots, so wlr-layer-shell works on both).
#
# Why a supervisor: piboy-osd is a Wayland client, so it dies WITH its
# compositor. An EmulationStation or compositor restart would make it vanish
# until the next boot. Here it is restarted for as long as the service runs.
#
# The System Settings menu must be able to push it aside while it is shown (same
# "overlay" layer): it creates /tmp/piboy-osd.pause and removes it on exit.

OSD_DIR=/userdata/system/piboy-osd
LOG=$OSD_DIR/osd.log
PAUSE=/tmp/piboy-osd.pause

export XDG_RUNTIME_DIR=/var/run

exec >>"$LOG" 2>&1
echo "=== $(date '+%F %T') piboy-osd: supervisor started ==="

# The socket name is NOT fixed: wayland-1 under sway, wayland-0 under labwc.
find_socket() {
	for s in "$XDG_RUNTIME_DIR"/wayland-[0-9]*; do
		case "$s" in *.lock) continue ;; esac
		[ -S "$s" ] || continue
		basename "$s"
		return 0
	done
	return 1
}

# Bounded log: the supervisor may restart often, no need to fill the SD card.
trim_log() {
	if [ -f "$LOG" ] && [ "$(wc -c <"$LOG" 2>/dev/null || echo 0)" -gt 262144 ]; then
		tail -c 65536 "$LOG" >"$LOG.tmp" 2>/dev/null && mv -f "$LOG.tmp" "$LOG"
	fi
}

while true; do
	trim_log

	# System Settings menu open: stay out of the way and wait.
	while [ -f "$PAUSE" ]; do sleep 1; done

	sock=""
	for _ in $(seq 1 60); do
		sock=$(find_socket) && break
		sleep 1
	done
	if [ -z "$sock" ]; then
		echo "$(date '+%F %T') no Wayland socket after 60 s, retrying"
		continue
	fi

	export WAYLAND_DISPLAY=$sock
	echo "$(date '+%F %T') starting on $WAYLAND_DISPLAY"

	# One instance at a time.
	killall piboy-osd 2>/dev/null
	sleep 0.3
	"$OSD_DIR/piboy-osd"
	echo "$(date '+%F %T') piboy-osd stopped (code $?), restarting in 3 s"
	sleep 3
done
