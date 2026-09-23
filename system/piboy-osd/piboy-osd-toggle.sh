#!/bin/sh
# Toggles the overlay (enabled 0 <-> 1). Picked up within ~1 s, no restart.
CONF=/userdata/system/piboy-osd.conf
cur=$(grep -E '^[[:space:]]*enabled[[:space:]]*=' "$CONF" | tail -1 | grep -oE '[01]' | head -1)
new=0; [ "$cur" = "0" ] && new=1
sed -i "s/^[[:space:]]*enabled[[:space:]]*=.*/enabled = $new/" "$CONF"
echo "piboy-osd enabled = $new"
