#!/bin/sh
# PiBoy DMG layer for stock Batocera (tested on 43.1, Raspberry Pi 4B and 3B).
# Also runs on a PiBoy XRS: the installer tells the two apart from the frames
# the controller chip sends, and on an XRS sets up the user-space driver in
# xpi-user/ (Batocera's kernel driver only speaks the DMG's protocol).
#
# Run ON THE CONSOLE, from a copy of this repository:
#   1. copy the folder to the console's Samba share, e.g. \\BATOCERA\share\piboy
#      (= /userdata/piboy on the console)
#   2. over SSH (root / linux):  sh /userdata/piboy/install.sh
#   3. reboot
#
# Idempotent: running it again updates the programs and keeps your settings.
# Never touches ROMs, BIOS, saves or scraped media.
#
# Options:
#   --no-m8       skip the Dirtywave M8 system
#   --no-netplay  skip the LAN netplay entries and announcement service
#   --no-wine     skip the StarCraft/Wine launchers (they need box64 + Wine,
#                 see wine/README.md; skipped automatically if Wine is absent)
#   --no-intro    keep Batocera's own boot splash and loading logo instead of
#                 the intro video

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
SYS=/userdata/system
ESCFG=$SYS/configs/emulationstation
XPI=/sys/kernel/xpi_gamecon
XU=/boot/xpi-user                   # PiBoy XRS: user-space driver
USERDRV=0; MODEL=dmg
if [ -f "$XU/enabled" ]; then
	USERDRV=1
	MODEL=$(cat "$XU/model" 2>/dev/null)
	[ -n "$MODEL" ] || MODEL=xrs
fi

# Copies xpi-user/ to /boot and starts the driver right away: once the MCU has
# had valid frames it expects them to keep coming (it is the Pi's heartbeat).
# The boot hook installed in step 2 starts it at every boot after this.
start_user_driver() {
	mount -o remount,rw /boot 2>/dev/null
	mkdir -p "$XU"
	cp "$HERE"/xpi-user/* "$XU"/
	echo "$MODEL" >"$XU/model"
	touch "$XU/enabled"
	sync
	mount -o remount,ro /boot 2>/dev/null
	setsid sh -c '
		while [ -f "$1/enabled" ] && [ ! -e /run/xpi_gamecon/stop ]; do
			python3 "$1/xpi_user.py" --daemon --model "$2" >>/tmp/xpi-user.log 2>&1
			rc=$?
			[ $rc -eq 3 ] || [ $rc -eq 4 ] && break
			sleep 1
		done' sh "$XU" "$MODEL" </dev/null >/dev/null 2>&1 &
	i=0
	while [ ! -f /run/xpi_gamecon/version ] && [ $i -lt 50 ]; do sleep 0.1; i=$((i + 1)); done
}

WITH_M8=1; WITH_NET=1; WITH_WINE=1; WITH_INTRO=1
for a in "$@"; do
	case "$a" in
		--no-m8) WITH_M8=0 ;;
		--no-netplay) WITH_NET=0 ;;
		--no-wine) WITH_WINE=0 ;;
		--no-intro) WITH_INTRO=0 ;;
		-h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "unknown option: $a"; exit 1 ;;
	esac
done

if [ ! -d /userdata ] || [ ! -f /usr/share/batocera/batocera.version ]; then
	echo "ERROR: run this on the Batocera console itself."
	exit 1
fi

echo "PiBoy layer installer (DMG / XRS)"
echo "  Batocera : $(cat /usr/share/batocera/batocera.version)"
echo "  Board    : $(tr -d '\0' </proc/device-tree/model 2>/dev/null)"
echo

# Windows editors and zip tools may have turned LF into CRLF: shell scripts
# with CRLF fail with confusing errors. Normalise the copy first.
find "$HERE" -type f \( -name '*.sh' -o -name '*.py' -o -name '*.conf' -o -name '*.xml' -o -name '*.keys' -o -name '*.ini' \) \
	-exec sed -i 's/\r$//' {} + 2>/dev/null
for f in "$HERE"/system/services/*; do sed -i 's/\r$//' "$f"; done

# ------------------------------------------------------------------ checks --
echo "[1/8] checks"
if [ "$USERDRV" = 1 ]; then
	# Batocera's xpi_gamecon speaks the DMG's protocol only, and would fight
	# the user-space driver over GPIO 26/27: never load it here.
	XPI=/run/xpi_gamecon
	# Set up by hand from xpi-user/README.md, maybe with an older version:
	# refresh it, then restart it (its boot loop starts the new file within a
	# second; the MCU grants 60 s when the old one stops).
	if ! cmp -s "$HERE/xpi-user/xpi_user.py" "$XU/xpi_user.py"; then
		mount -o remount,rw /boot 2>/dev/null
		cp "$HERE"/xpi-user/* "$XU"/
		sync
		mount -o remount,ro /boot 2>/dev/null
		echo "  xpi-user driver updated"
		old=$(cat "$XPI/pid" 2>/dev/null)
		if [ -n "$old" ] && [ -d "/proc/$old" ]; then
			kill "$old"
			i=0
			while [ $i -lt 50 ]; do
				new=$(cat "$XPI/pid" 2>/dev/null)
				[ -n "$new" ] && [ "$new" != "$old" ] && [ -d "/proc/$new" ] && break
				sleep 0.1; i=$((i + 1))
			done
			sleep 1
		fi
	fi
	if [ ! -f "$XPI/version" ]; then
		echo "  WARNING: the xpi-user driver is enabled but not running. Reboot once,"
		echo "  check that the controls work, then run this installer again."
		echo "  Its log: /tmp/xpi-user.log"
	fi
else
	modprobe xpi_gamecon 2>/dev/null
	sleep 1
	# The module also loads on an XRS, but never decodes a frame there
	# (version stays 0): ask the chip which frame length it sends.
	if [ ! -d "$XPI" ] || [ "$(cat "$XPI/version" 2>/dev/null)" = 0 ]; then
		rmmod xpi_gamecon 2>/dev/null
		MODEL=$(python3 "$HERE/xpi-user/xpi_user.py" --probe 2>/dev/null)
		case "$MODEL" in
			xrs|dmg)
				echo "  PiBoy $(echo "$MODEL" | tr a-z A-Z) detected: installing the xpi-user driver"
				start_user_driver
				USERDRV=1
				XPI=/run/xpi_gamecon
				;;
			*)
				echo "  ERROR: the controller chip answers neither as a DMG nor as an XRS."
				echo "  Is this a PiBoy? Check: dmesg | grep -i gamecon; tail /tmp/xpi-user.log"
				exit 1
				;;
		esac
	fi
fi
if [ "$USERDRV" = 1 ]; then
	echo "  console  : PiBoy $(echo "$MODEL" | tr a-z A-Z), user-space driver (xpi-user)"
else
	echo "  console  : PiBoy DMG, Batocera's xpi_gamecon kernel driver"
fi
if [ -d "$XPI" ]; then
	# The MCU reports its firmware as 0xMmp (262 = 0x106 = 1.0.6).
	FW=$(cat "$XPI/version" 2>/dev/null)
	FWTXT=$(printf '%x' "${FW:-0}" 2>/dev/null | sed 's/^\(.\)\(.\)\(.\)$/\1.\2.\3/')
	echo "  controller driver running ($XPI), PiBoy MCU firmware $FWTXT"
	if [ "$MODEL" = dmg ] && [ -n "$FW" ] && [ "$FW" -lt 262 ] 2>/dev/null; then
		echo "  WARNING: firmware older than 1.0.6, the last release for the DMG."
		echo "  1.0.6 fixes reboot/shutdown issues and the joystick calibration."
		echo "  The firmware and Experimental Pi's updater are in the firmware/ folder"
		echo "  of this repository (see firmware/README.md)."
	fi
else
	echo "  WARNING: /sys/kernel/xpi_gamecon is missing. The PiBoy driver ships in"
	echo "  the official Pi 3/Pi 4 images; check: modinfo xpi_gamecon; dmesg | grep -i gamecon"
fi
# Batocera's own "PIBOY" power-switch option starts the vendor's old fan/audio/
# power scripts, which would fight with the piboy service over the fan and the MCU.
if [ "$(batocera-settings-get system.power.switch 2>/dev/null)" = "PIBOY" ]; then
	batocera-settings-set system.power.switch "" 2>/dev/null
	echo "  system.power.switch=PIBOY disabled (replaced by the piboy service)"
fi

# ------------------------------------------------------------------- /boot --
echo "[2/8] boot partition (config.txt, early boot hook)"
mount -o remount,rw /boot 2>/dev/null
# Display. Stock Batocera loads the full KMS driver (dtoverlay=vc4-kms-v3d):
# it takes the screen over right after the splash and cannot drive the DPI
# panel, so the console shows the splash, then stays black while ES runs.
# A config.txt that already drives the panel is left alone.
FIXED_CFG=0
if ! grep -q '^[[:space:]]*dpi_timings' /boot/config.txt 2>/dev/null; then
	echo "  WARNING: no PiBoy display block in /boot/config.txt, the internal screen"
	echo "  will stay black. Paste boot/config-piboy-pi4.txt (or -pi3.txt) at the end"
	echo "  of config.txt, see step 2 of the README."
elif grep -q '^[[:space:]]*dtoverlay=vc4-kms-v3d' /boot/config.txt; then
	[ -f /boot/config.txt.orig ] || cp /boot/config.txt /boot/config.txt.orig
	sed -i 's/^\([[:space:]]*\)\(dtoverlay=vc4-kms-v3d.*\)$/\1#\2   # disabled for the PiBoy DPI screen/' /boot/config.txt &&
		FIXED_CFG=1 && echo "  config.txt: dtoverlay=vc4-kms-v3d disabled (it blanks the screen after the splash)"
else
	echo "  config.txt: PiBoy display block present"
fi
# The hook publishes the battery before EmulationStation starts (ES looks for
# it only once) and tells the MCU it may cut power on shutdown.
if [ -f /boot/boot-custom.sh ] && ! grep -q 'xpi_gamecon' /boot/boot-custom.sh; then
	cp -a /boot/boot-custom.sh /boot/boot-custom.sh.before-piboy
	echo "  your previous boot-custom.sh was saved as boot-custom.sh.before-piboy"
fi
cp "$HERE/boot/boot-custom.sh" /boot/boot-custom.sh && chmod +x /boot/boot-custom.sh && echo "  installed"
# Intro: the hook drops Batocera's boot logo (the intro video follows) and puts
# the "loading..." screen in place of EmulationStation's logo, from here.
if [ "$WITH_INTRO" = 1 ]; then
	mkdir -p /boot/branding && cp "$HERE"/intro/branding/* /boot/branding/ && echo "  intro: boot logos"
fi
sync
mount -o remount,ro /boot 2>/dev/null

# ---------------------------------------------------------- daemon, configs --
echo "[3/8] piboy service (battery gauge, fan, LED, power switch)"
mkdir -p "$SYS/services"
for f in piboy-dmgcontrol.py piboy-mouse.py piboy-led.sh wifi-watchdog.sh; do
	cp "$HERE/system/$f" "$SYS/$f"
done
chmod +x "$SYS/piboy-led.sh" "$SYS/wifi-watchdog.sh"
# Settings are yours once installed: never overwrite an existing file, only
# add keys that a newer version introduced.
for f in piboy-fan.conf piboy-led.conf piboy-power.conf piboy-osd.conf; do
	if [ ! -f "$SYS/$f" ]; then
		cp "$HERE/system/conf/$f" "$SYS/$f" && echo "  $f"
	else
		echo "  $f (kept)"
	fi
done
# Options added by newer versions: appended with their default value.
if [ -f "$SYS/piboy-power.conf" ]; then
	for kv in "save_on_shutdown = 1" "low_battery_warning = 10" "screen_off_standby = 1"; do
		grep -q "^[[:space:]]*${kv%% *}[[:space:]]*=" "$SYS/piboy-power.conf" || echo "$kv" >>"$SYS/piboy-power.conf"
	done
fi
if [ -f "$SYS/piboy-osd.conf" ]; then
	grep -q '^[[:space:]]*bluetooth_mode' "$SYS/piboy-osd.conf" || echo "bluetooth_mode = es" >>"$SYS/piboy-osd.conf"
	grep -q '^[[:space:]]*language' "$SYS/piboy-osd.conf" || echo "language = en" >>"$SYS/piboy-osd.conf"
fi
for s in piboy piboyosd wifiwatchdog; do
	cp "$HERE/system/services/$s" "$SYS/services/$s"
done
chmod +x "$SYS"/services/*

# --------------------------------------------------------------------- OSD --
echo "[4/8] in-game OSD + System Settings menu"
mkdir -p "$SYS/piboy-osd/resources"
# A running binary cannot be overwritten ("text file busy").
[ -x "$SYS/services/piboyosd" ] && "$SYS/services/piboyosd" stop >/dev/null 2>&1
killall piboy-osd piboy-settings 2>/dev/null
sleep 1
cp "$HERE"/system/piboy-osd/piboy-osd "$HERE"/system/piboy-osd/piboy-settings "$SYS/piboy-osd/"
cp "$HERE"/system/piboy-osd/*.sh "$SYS/piboy-osd/"
cp "$HERE"/system/piboy-osd/resources/* "$SYS/piboy-osd/resources/"
chmod +x "$SYS"/piboy-osd/piboy-osd "$SYS"/piboy-osd/piboy-settings "$SYS"/piboy-osd/*.sh
mkdir -p /userdata/roms/reglages
cp "$HERE"/roms/reglages/* /userdata/roms/reglages/
chmod +x /userdata/roms/reglages/*.sh

# ------------------------------------------------ EmulationStation integration --
echo "[5/8] EmulationStation (screen-off standby, pad mapping, systems)"
mkdir -p "$ESCFG/scripts/screensaver-start" "$ESCFG/scripts/screensaver-stop"
# The Pi 3 needs the "signal before power" variant (see es/scripts-pi3).
SCR="$HERE/es/scripts"
[ "$(cat /boot/boot/batocera.board 2>/dev/null)" = "bcm2837" ] && SCR="$HERE/es/scripts-pi3"
cp "$SCR/screensaver-start/10-screen-off.sh" "$ESCFG/scripts/screensaver-start/"
cp "$SCR/screensaver-stop/10-screen-on.sh" "$ESCFG/scripts/screensaver-stop/"
chmod +x "$ESCFG"/scripts/*/*.sh

# PiBoy pad mapping, so ES does not ask to configure it on first boot.
PADXML=piboy-input.xml
[ "$MODEL" = xrs ] && PADXML=piboy-xrs-input.xml
HERE="$HERE" ESCFG="$ESCFG" PADXML="$PADXML" python3 - <<'PY'
import os, xml.etree.ElementTree as ET
dst = os.path.join(os.environ['ESCFG'], 'es_input.cfg')
new = ET.fromstring(open(os.path.join(os.environ['HERE'], 'es', os.environ['PADXML']), encoding='utf-8').read())
if os.path.exists(dst):
    tree = ET.parse(dst); root = tree.getroot()
else:
    root = ET.Element('inputList'); tree = ET.ElementTree(root)
# Both PiBoy pads have the same GUID: match the name too.
if any(c.get('deviceGUID') == new.get('deviceGUID') and c.get('deviceName') == new.get('deviceName')
       for c in root.findall('inputConfig')):
    print('  pad mapping already present')
else:
    root.append(new); tree.write(dst, encoding='utf-8', xml_declaration=True)
    print('  pad mapping added')
PY

# es_systems_custom.cfg REPLACES the system list on Batocera 43, it does not
# extend it: it must hold the full stock list plus our systems.
BLOCKS="$HERE/es/system-settings.xml"
[ "$WITH_M8" = 1 ] && BLOCKS="$BLOCKS $HERE/es/m8-system.xml"
BLOCKS="$BLOCKS" ESCFG="$ESCFG" python3 - <<'PY'
import os, re, shutil
dst = os.path.join(os.environ['ESCFG'], 'es_systems_custom.cfg')
stock = '/usr/share/emulationstation/es_systems.cfg'
if os.path.exists(dst) and '<system>' in open(dst, encoding='utf-8').read():
    base = open(dst, encoding='utf-8').read()
    shutil.copy2(dst, dst + '.before-piboy')
else:
    base = open(stock, encoding='utf-8').read()
added = []
for path in os.environ['BLOCKS'].split():
    block = open(path, encoding='utf-8').read()
    name = re.search(r'<name>([^<]+)</name>', block).group(1)
    if '<name>%s</name>' % name in base:
        continue
    base = base.replace('</systemList>', block + '</systemList>', 1)
    added.append(name)
# Older versions of these blocks quoted %ROM%. ES already escapes the path
# (System\ Settings.sh), so the quotes kept the backslash and bash could not
# find any launcher whose name has a space: fix them in place.
old = '<command>bash "%ROM%"</command>'
fixed = base.count(old)
base = base.replace(old, '<command>bash %ROM%</command>')
open(dst, 'w', encoding='utf-8').write(base)
print('  systems added: %s' % (', '.join(added) if added else 'none (already there)'))
if fixed:
    print('  launch command fixed in %d existing system(s)' % fixed)
PY

# Intro video. Batocera plays a random .mp4 from /userdata/splash; one of
# yours with the same name is kept aside, under a name it does not play.
if [ "$WITH_INTRO" = 1 ]; then
	mkdir -p /userdata/splash
	if [ -f /userdata/splash/splash.mp4 ] && ! cmp -s "$HERE/intro/splash.mp4" /userdata/splash/splash.mp4; then
		mv /userdata/splash/splash.mp4 /userdata/splash/splash.mp4.before-piboy
		echo "  your splash.mp4 was kept as /userdata/splash/splash.mp4.before-piboy"
	fi
	cp "$HERE/intro/splash.mp4" /userdata/splash/splash.mp4 && echo "  intro video"
	others=$(find /userdata/splash -maxdepth 1 -iname '*.mp4' ! -name splash.mp4 | wc -l)
	[ "$others" -gt 0 ] && echo "  note: $others other video(s) in /userdata/splash, Batocera picks one at random"
fi

# ------------------------------------------------------------ extras ---------
echo "[6/8] Ports: file manager (stick = mouse)"
mkdir -p /userdata/roms/ports
cp "$HERE/roms/ports/FileManager.sh" "$HERE/roms/ports/FileManager.sh.keys" /userdata/roms/ports/

if [ "$WITH_NET" = 1 ]; then
	echo "      Ports: LAN netplay (host / join) + announcement service"
	cp "$HERE/system/piboy-netplay.sh" "$HERE/system/piboy-netplay-announce.py" "$HERE/system/piboy-netplay-find.py" "$SYS/"
	chmod +x "$SYS/piboy-netplay.sh"
	for f in piboy-netplay.conf piboy-netplay-cores.conf; do
		[ -f "$SYS/$f" ] || cp "$HERE/system/conf/$f" "$SYS/$f"
	done
	cp "$HERE/system/services/piboynetplay" "$SYS/services/"
	cp "$HERE/roms/ports/Netplay - Host.sh" "$HERE/roms/ports/Netplay - Join.sh" /userdata/roms/ports/
fi

if [ "$WITH_M8" = 1 ]; then
	echo "      M8 Tracker system (m8c)"
	mkdir -p "$SYS/m8c/presets" /userdata/roms/m8 "$SYS/.local/share/m8c"
	cp "$HERE/m8/bin/m8c" "$HERE/m8/m8c.sh" "$HERE/m8/m8c-quit-watch.py" "$SYS/m8c/"
	cp "$HERE"/m8/presets/* "$SYS/m8c/presets/"
	chmod +x "$SYS/m8c/m8c" "$SYS/m8c/m8c.sh"
	[ -f "$SYS/.local/share/m8c/config.ini" ] || cp "$HERE/m8/config.ini" "$SYS/.local/share/m8c/"
	cp "$HERE/m8/gamecontrollerdb.txt" "$SYS/.local/share/m8c/"
	cp "$HERE"/m8/roms/*.sh /userdata/roms/m8/
	chmod +x /userdata/roms/m8/*.sh
fi

if [ "$WITH_WINE" = 1 ] && [ -x "$SYS/box64/box64" ] && [ -d "$SYS/wine/current" ]; then
	echo "      StarCraft through box64 + Wine"
	mkdir -p "$SYS/wine"
	cp "$HERE/wine/winebox.sh" "$HERE/wine/wine-quit.sh" "$HERE/wine/starcraft.sh" "$SYS/wine/"
	chmod +x "$SYS"/wine/*.sh
	cp "$HERE/wine/ports/StarCraft.sh" /userdata/roms/ports/
elif [ "$WITH_WINE" = 1 ]; then
	echo "      (box64/Wine not found: StarCraft launcher skipped, see wine/README.md)"
fi
chmod +x /userdata/roms/ports/*.sh 2>/dev/null

# RetroArch network commands (local UDP port 55355): used to save the running
# game before a power-switch or empty-battery shutdown, and for messages.
if [ -z "$(batocera-settings-get global.retroarch.network_cmd_enable 2>/dev/null)" ]; then
	batocera-settings-set global.retroarch.network_cmd_enable true 2>/dev/null
	echo "      RetroArch network commands enabled (save before shutdown)"
fi

# ---------------------------------------------------------------- services --
echo "[7/8] enabling services"
for s in piboy piboyosd wifiwatchdog; do
	batocera-services enable "$s" >/dev/null 2>&1 && echo "  $s"
done
[ "$WITH_NET" = 1 ] && batocera-services enable piboynetplay >/dev/null 2>&1 && echo "  piboynetplay"

echo "[8/8] done"
if [ "$FIXED_CFG" = 1 ]; then
	echo
	echo "config.txt was fixed: the internal screen comes back after the reboot."
fi
cat <<'END'

Reboot to start everything:

    reboot

Then:
  - EmulationStation > System Settings: OSD, LED, fan, CPU, Wi-Fi, Bluetooth
  - Menu > UI settings > Screensaver: pick a delay; the screen really turns off
  - checks:  cat /sys/class/power_supply/BAT0/capacity
             tail /userdata/system/piboy-dmgcontrol.log

The battery gauge is calibrated for a ~4000 mAh pack (the stock PiBoy cell
measures about that, not the 4900 on its label). If yours differs, change
BATT_CAPACITY_MAH at the top of /userdata/system/piboy-dmgcontrol.py.
END
