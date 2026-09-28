#!/bin/sh
# ES screensaver stop: turn the display back on.
XPI=/sys/kernel/xpi_gamecon
[ -d "$XPI" ] || XPI=/run/xpi_gamecon   # PiBoy XRS: xpi-user driver
echo 1 > "$XPI/flags"
