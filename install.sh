#!/bin/sh
# PiBoy DMG layer for stock Batocera (tested on 43.1, Raspberry Pi 4B and 3B).
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

set -u

HERE=$(cd "$(dirname "$0")" && pwd)
SYS=/userdata/system
ESCFG=$SYS/configs/emulationstation
XPI=/sys/kernel/xpi_gamecon

WITH_M8=1; WITH_NET=1; WITH_WINE=1
for a in "$@"; do
	case "$a" in
		--no-m8) WITH_M8=0 ;;
		--no-netplay) WITH_NET=0 ;;
		--no-wine) WITH_WINE=0 ;;
		-h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
		*) echo "unknown option: $a"; exit 1 ;;
	esac
done

if [ ! -d /userdata ] || [ ! -f /usr/share/batocera/batocera.version ]; then
	echo "ERROR: run this on the Batocera console itself."
	exit 1
fi

echo "PiBoy DMG layer installer"
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
modprobe xpi_gamecon 2>/dev/null
if [ -d "$XPI" ]; then
	# The MCU reports its firmware as 0xMmp (262 = 0x106 = 1.0.6).
	FW=$(cat "$XPI/version" 2>/dev/null)
	FWTXT=$(printf '%x' "${FW:-0}" 2>/dev/null | sed 's/^\(.\)\(.\)\(.\)$/\1.\2.\3/')
	echo "  xpi_gamecon driver loaded, PiBoy MCU firmware $FWTXT"
	if [ -n "$FW" ] && [ "$FW" -lt 262 ] 2>/dev/null; then
		echo "  WARNING: firmware older than 1.0.6, the last release for the DMG."
		echo "  1.0.6 fixes reboot/shutdown issues and the joystick calibration."
		echo "  Experimental Pi's updater (Windows tool, or loader.py over USB) is"
		echo "  preserved at https://archive.org/details/EXPPI"
	fi
else
	echo "  WARNING: /sys/kernel/xpi_gamecon is missing. The PiBoy driver ships in"
	echo "  the official Pi 3/Pi 4 images; check: modinfo xpi_gamecon; dmesg | grep -i gamecon"
fi
if grep -q 'dpi_timings' /boot/config.txt 2>/dev/null; then
	echo "  config.txt: PiBoy display block present"
else
	echo "  WARNING: no dpi_timings in /boot/config.txt. If you read this over SSH"
	echo "  with a black screen, append boot/config-piboy-pi4.txt (or -pi3.txt)."
fi
# Batocera's own "PIBOY" power-switch option starts the vendor's old fan/audio/
# power scripts, which would fight with the piboy service over the fan and the MCU.
if [ "$(batocera-settings-get system.power.switch 2>/dev/null)" = "PIBOY" ]; then
	batocera-settings-set system.power.switch "" 2>/dev/null
	echo "  system.power.switch=PIBOY disabled (replaced by the piboy service)"
fi

# ------------------------------------------------------------------- /boot --
echo "[2/8] early boot hook (/boot/boot-custom.sh)"
# Publishes the battery before EmulationStation starts (ES looks for it only
# once) and tells the MCU it may cut power on shutdown.
mount -o remount,rw /boot 2>/dev/null
if [ -f /boot/boot-custom.sh ] && ! grep -q 'xpi_gamecon' /boot/boot-custom.sh; then
	cp -a /boot/boot-custom.sh /boot/boot-custom.sh.before-piboy
	echo "  your previous boot-custom.sh was saved as boot-custom.sh.before-piboy"
fi
cp "$HERE/boot/boot-custom.sh" /boot/boot-custom.sh && chmod +x /boot/boot-custom.sh && echo "  installed"
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
HERE="$HERE" ESCFG="$ESCFG" python3 - <<'PY'
import os, xml.etree.ElementTree as ET
dst = os.path.join(os.environ['ESCFG'], 'es_input.cfg')
new = ET.fromstring(open(os.path.join(os.environ['HERE'], 'es', 'piboy-input.xml'), encoding='utf-8').read())
if os.path.exists(dst):
    tree = ET.parse(dst); root = tree.getroot()
else:
    root = ET.Element('inputList'); tree = ET.ElementTree(root)
if any(c.get('deviceGUID') == new.get('deviceGUID') for c in root.findall('inputConfig')):
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
open(dst, 'w', encoding='utf-8').write(base)
print('  systems added: %s' % (', '.join(added) if added else 'none (already there)'))
PY

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
