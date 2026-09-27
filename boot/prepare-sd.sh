#!/bin/sh
# Prepare a Batocera boot partition for the PiBoy DMG (Raspberry Pi 4B, 3B/3B+).
#
# Usage:
#   sh prepare-sd.sh /Volumes/BATOCERA         macOS, SD card in a reader
#   sh prepare-sd.sh /media/$USER/BATOCERA     Linux, SD card in a reader
#   sh prepare-sd.sh --live                    on the console itself (/boot)
#   Windows: use prepare-sd.ps1 from the same folder.
#
# It only edits config.txt:
#   - the untouched file is saved once as config.txt.orig;
#   - "dtoverlay=vc4-kms-v3d" is commented out. Stock Batocera loads this full
#     KMS display driver; it takes the screen over right after the splash and
#     cannot drive the PiBoy's DPI panel, so you get the splash, then black;
#   - any previous PiBoy block is removed (ours, or the one Batocera writes
#     itself for its "PIBOY" power switch option), then the block for your Pi
#     is appended at the end.
#
# The Pi model comes from the image (boot/batocera.board: bcm2711 = Pi 4,
# bcm2837 = Pi 3). Override with --pi4 or --pi3. Safe to run again.

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
BOOT=""
LIVE=0
PI=""

while [ $# -gt 0 ]; do
	case "$1" in
		--live) LIVE=1; BOOT=/boot ;;
		--pi4) PI=4 ;;
		--pi3) PI=3 ;;
		-h|--help) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		-*) echo "unknown option: $1"; exit 1 ;;
		*) BOOT=$1 ;;
	esac
	shift
done

if [ -z "$BOOT" ]; then
	echo "usage: sh prepare-sd.sh <BATOCERA partition>   or   sh prepare-sd.sh --live"
	exit 1
fi
BOOT=${BOOT%/}
CFG="$BOOT/config.txt"
if [ ! -f "$CFG" ] || [ ! -f "$BOOT/batocera-boot.conf" ]; then
	echo "ERROR: $BOOT does not look like a Batocera boot partition"
	echo "       (config.txt and batocera-boot.conf expected). Nothing written."
	exit 1
fi

if [ -z "$PI" ]; then
	BOARD=$(tr -d '\r\n ' <"$BOOT/boot/batocera.board" 2>/dev/null)
	case "$BOARD" in
		bcm2711) PI=4 ;;
		bcm2837|bcm2836) PI=3 ;;
		bcm2712)
			echo "ERROR: this is the Raspberry Pi 5 image (bcm2712). The PiBoy DMG"
			echo "       needs a Pi 4 or Pi 3 and the matching image: bcm2711 or bcm2837."
			exit 1 ;;
		*)
			echo "ERROR: unknown board '${BOARD:-none}' in $BOOT/boot/batocera.board."
			echo "       Add --pi4 or --pi3 if you are sure of your image."
			exit 1 ;;
	esac
fi
BLOCK="$HERE/config-piboy-pi$PI.txt"
[ -f "$BLOCK" ] || { echo "ERROR: $BLOCK is missing"; exit 1; }
echo "Target: $CFG (Raspberry Pi $PI)"

if [ "$LIVE" = 1 ]; then
	mount -o remount,rw /boot 2>/dev/null
fi

if [ ! -f "$CFG.orig" ]; then
	cp "$CFG" "$CFG.orig" && echo "  original saved as config.txt.orig"
fi

# Rebuilt in a temporary file, checked, then moved in place: a truncated
# config.txt is a console that no longer boots.
TMP="$BOOT/config.txt.piboy-new"
tr -d '\r' <"$CFG" |
sed -e '/^# ====== PiBoy DMG - Raspberry Pi/,/^# ====== end of PiBoy DMG block/d' \
    -e '/^# ====== PiBoy Case setup section/,/^# ====== PiBoy Case toggle section/d' \
    -e 's/^\([[:space:]]*\)\(dtoverlay=vc4-kms-v3d.*\)$/\1#\2   # disabled for the PiBoy DPI screen/' \
    >"$TMP" || { rm -f "$TMP"; echo "ERROR: could not write $TMP"; exit 1; }
# Drop trailing blank lines left by a removed block, then append ours.
awk '/^[[:space:]]*$/ { n++; next } { for (; n > 0; n--) print ""; print }' "$TMP" >"$TMP.2" &&
	mv -f "$TMP.2" "$TMP"
printf '\n' >>"$TMP"
tr -d '\r' <"$BLOCK" >>"$TMP"

if grep -q '^enable_dpi_lcd=1' "$TMP" &&
   grep -q '^# ====== end of PiBoy DMG block' "$TMP" &&
   ! grep -q '^[[:space:]]*dtoverlay=vc4-kms-v3d' "$TMP"; then
	mv -f "$TMP" "$CFG"
	echo "  vc4-kms-v3d disabled, PiBoy block for the Pi $PI written"
	RC=0
else
	rm -f "$TMP"
	echo "ERROR: the new file failed its check, config.txt left untouched"
	RC=1
fi

sync
if [ "$LIVE" = 1 ]; then
	mount -o remount,ro /boot 2>/dev/null
	[ "$RC" = 0 ] && echo "Done. Reboot to apply."
else
	[ "$RC" = 0 ] && echo "Done. Eject the card cleanly, then boot the console."
fi
exit "$RC"
