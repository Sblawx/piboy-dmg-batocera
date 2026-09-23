#!/bin/bash
# StarCraft 1.16.1 through box64 + Wine, in a 640x480 Wine desktop (the PiBoy's
# native resolution). Starts the stick-to-mouse daemon with StarCraft shortcuts
# for the duration of the game only, and switches the CPU to performance.
SC=/userdata/roms/windows/StarCraft
MOUSE=/userdata/system/piboy-mouse.py
export DISPLAY="${DISPLAY:-:0.0}"

set_gov() { for c in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
              echo "$1" > "$c" 2>/dev/null; done; }
GOV_OLD=$(cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null)
GOV_OLD=${GOV_OLD:-schedutil}
MOUSE_PID=""
cleanup() {
  [ -n "$MOUSE_PID" ] && kill "$MOUSE_PID" 2>/dev/null
  set_gov "$GOV_OLD"
  batocera-mouse hide 2>/dev/null
}
trap cleanup EXIT INT TERM

pkill -9 -f StarCraft.exe 2>/dev/null
pkill -9 -f wineserver 2>/dev/null
sleep 1
set_gov performance
batocera-mouse show 2>/dev/null
python3 "$MOUSE" --starcraft >/userdata/system/piboy-mouse.log 2>&1 &
MOUSE_PID=$!
sleep 1                     # let the compositor register the virtual mouse
cd "$SC"
# Wine's output goes to a log: its background services (services.exe,
# winedevice, rpcss...) would otherwise inherit emulatorlauncher's pipe, which
# would wait for them forever - ES would stay on "loading" after quitting.
/userdata/system/wine/winebox.sh explorer /desktop=sc,640x480 "$SC/StarCraft.exe" >/userdata/system/wine/sc-run.log 2>&1 </dev/null
# wineserver -k ends the whole session cleanly; the pkills are a safety net.
sh /userdata/system/wine/wine-quit.sh 2>/dev/null
pkill -9 -f 'lib/wine/x86_64-unix/wine' 2>/dev/null
pkill -9 -f wineserver 2>/dev/null
