#!/bin/sh
# ES screensaver start, Pi 3 variant: turns the panel off (~450 mA saved).
#
# Order MATTERS: some PiBoy panels do not lock onto a DPI signal that is already
# running when their power comes back (white vertical lines, horizontal lines or
# a black screen on wake). So the SIGNAL goes off first (compositor output off),
# then the POWER (flags=0). Waking does the reverse, like a cold boot.
# Installed by default on the Pi 3; also worth trying on a Pi 4 whose screen
# stays black after standby.
export XDG_RUNTIME_DIR=/var/run
[ -n "$WAYLAND_DISPLAY" ] || WAYLAND_DISPLAY=$(ls /var/run 2>/dev/null | grep -E '^wayland-[0-9]+$' | head -1)
export WAYLAND_DISPLAY

OUT=$(wlr-randr 2>/dev/null | awk '/^[A-Za-z]/ {print $1; exit}')
[ -n "$OUT" ] && wlr-randr --output "$OUT" --off 2>/dev/null && sleep 0.3

echo 0 > /sys/kernel/xpi_gamecon/flags
