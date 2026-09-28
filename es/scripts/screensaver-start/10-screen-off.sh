#!/bin/sh
# ES screensaver start: turn the display off through the MCU (~450 mA saved).
# Option "Screen off in standby" of the System Settings menu (piboy-power.conf).
grep -Eq '^[[:space:]]*screen_off_standby[[:space:]]*=[[:space:]]*0' /userdata/system/piboy-power.conf 2>/dev/null && exit 0
XPI=/sys/kernel/xpi_gamecon
[ -d "$XPI" ] || XPI=/run/xpi_gamecon   # PiBoy XRS: xpi-user driver
echo 0 > "$XPI/flags"
