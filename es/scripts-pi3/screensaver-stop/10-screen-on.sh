#!/bin/sh
# ES screensaver stop, Pi 3 variant: POWER first, a short delay for the panel to
# initialise, THEN the SIGNAL, so the panel gets a fresh modeset like at boot.
export XDG_RUNTIME_DIR=/var/run
[ -n "$WAYLAND_DISPLAY" ] || WAYLAND_DISPLAY=$(ls /var/run 2>/dev/null | grep -E '^wayland-[0-9]+$' | head -1)
export WAYLAND_DISPLAY

echo 1 > /sys/kernel/xpi_gamecon/flags
sleep 0.5

OUT=$(wlr-randr 2>/dev/null | awk '/^[A-Za-z]/ {print $1; exit}')
[ -n "$OUT" ] && wlr-randr --output "$OUT" --on 2>/dev/null
