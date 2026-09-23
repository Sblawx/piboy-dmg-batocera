#!/bin/sh
# LAN netplay between two PiBoys (or any two Batocera consoles).
#
#   piboy-netplay.sh join   -> looks for a hosted game on the LAN and joins it
#   piboy-netplay.sh host   -> hosts the game set in piboy-netplay.conf
#                              (fallback only: the normal way to host is
#                              EmulationStation's own menu, long press A on a
#                              game -> netplay -> host)
#
# Nothing to configure on the joining side: the host announces the game, the
# core, its address and port (piboy-netplay-announce.py). Changing game is done
# on the host only.
#
# EmulationStation normally passes the -p1* controller arguments; a Ports script
# does not receive them, so they are rebuilt here, otherwise the pad is not
# mapped in game.

set -u

CONF=/userdata/system/piboy-netplay.conf
LOG=/userdata/system/piboy-netplay.log
FIND=/userdata/system/piboy-netplay-find.py
PAD_NAME="PiBoy DMG Controller"
PAD_GUID=15000000010000000100000000010000
WAIT=12

MODE=${1:-}
case "$MODE" in client) MODE=join ;; esac

log() { echo "[netplay] $(date '+%m-%d %H:%M:%S') $*" >>"$LOG"; }
# On-screen message through EmulationStation's local API.
notify() { curl -s -m 2 -X POST --data-binary "Netplay: $*" http://127.0.0.1:1234/notify >/dev/null 2>&1; }

case "$MODE" in
	host|join) ;;
	*) log "unknown role: '$MODE' (expected host or join)"; exit 1 ;;
esac

# Reads "key = value" from the config. The first name is the English key, the
# second the older French one, kept so existing config files keep working.
val() {
	[ -f "$CONF" ] || return
	for k in "$@"; do
		v=$(sed -n "s/^[[:space:]]*$k[[:space:]]*=[[:space:]]*//p" "$CONF" | tail -1 | sed -e 's/[[:space:]]*$//')
		[ -n "$v" ] && { printf '%s' "$v"; return; }
	done
}

if [ "$MODE" = join ]; then
	log "looking for a game on the local network..."
	# The finder shows its own on-screen message when nothing can be joined.
	INFO=$(python3 "$FIND" "$WAIT") || {
		log "nothing to join (see the [find] lines above)"
		exit 1
	}
	SYS=$(printf '%s' "$INFO" | cut -f1)
	ROM=$(printf '%s' "$INFO" | cut -f2)
	CORE=$(printf '%s' "$INFO" | cut -f3)
	HOST=$(printf '%s' "$INFO" | cut -f4)
	PORT=$(printf '%s' "$INFO" | cut -f5)
else
	SYS=$(val system systeme)
	ROM=$(val rom)
	CORE=$(val core coeur)
	PORT=$(val port)
	[ -n "$PORT" ] || PORT=55435
	if [ -z "$SYS" ] || [ -z "$ROM" ] || [ ! -f "$ROM" ]; then
		log "host fallback: invalid system or rom in $CONF"
		notify "set a valid system and rom in piboy-netplay.conf"
		exit 1
	fi
fi

# evdev path of the pad, found by name.
PAD=""
for d in /sys/class/input/event*; do
	[ -r "$d/device/name" ] || continue
	[ "$(cat "$d/device/name")" = "$PAD_NAME" ] || continue
	PAD=/dev/input/$(basename "$d")
	break
done
[ -n "$PAD" ] || log "PiBoy pad not found, launching without -p1devicepath"

set -- -p1index 0 -p1guid "$PAD_GUID" -p1name "$PAD_NAME" \
       -p1nbbuttons 20 -p1nbhats 1 -p1nbaxes 2
[ -n "$PAD" ] && set -- "$@" -p1devicepath "$PAD"
set -- "$@" -system "$SYS" -rom "$ROM"
# Both sides MUST run the same core or RetroArch refuses the connection: when
# joining it comes from the announcement, when hosting from the config file.
[ -n "$CORE" ] && set -- "$@" -emulator libretro -core "$CORE"

if [ "$MODE" = host ]; then
	set -- "$@" -netplaymode host -netplayport "$PORT"
	log "HOSTING  $SYS / $(basename "$ROM") core=${CORE:-default} port $PORT"
else
	set -- "$@" -netplaymode client -netplayip "$HOST" -netplayport "$PORT"
	log "JOINING  $HOST:$PORT -> $SYS / $(basename "$ROM") core=$CORE"
fi

exec /usr/bin/emulatorlauncher "$@"
