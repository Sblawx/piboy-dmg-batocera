#!/bin/sh
# ES screensaver start: turn the display off through the MCU (~450 mA saved).
echo 0 > /sys/kernel/xpi_gamecon/flags
