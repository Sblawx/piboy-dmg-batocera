#!/bin/bash
# piboy-led.sh - sets the PiBoy's bicolour power LED directly (via xpi_gamecon).
# For a permanent setting use piboy-led.conf or the System Settings menu instead:
# the piboy service re-applies the conf and would override this.
# Usage: piboy-led.sh <red 0-255> <green 0-255>
#        piboy-led.sh get
XPI=/sys/kernel/xpi_gamecon
clamp(){ v=$1; [ $v -lt 0 ] && v=0; [ $v -gt 255 ] && v=255; echo $v; }
if [ "$1" = get ]; then echo "red=$(cat $XPI/red) green=$(cat $XPI/green)"; exit 0; fi
if [ -z "$2" ]; then echo "usage: $0 <red 0-255> <green 0-255> | get"; exit 1; fi
R=$(clamp $1); G=$(clamp $2)
echo $R > $XPI/red; echo $G > $XPI/green
echo "set red=$R green=$G"
