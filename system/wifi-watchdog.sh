#!/bin/sh
# Wi-Fi watchdog for Batocera (connman).
#
# Seen on the Pi 4: under sustained transmission (rsync at ~9 MB/s) the 5 GHz
# link died twice (after ~13 then ~15 min) - and the sneaky part is that NOTHING
# noticed. No kernel message, no connman message, and when connman was restarted
# by hand it found wlan0 <UP,RUNNING,LOWER_UP>: for the driver the card was still
# associated. A "zombie" link: carrier present, no packet goes through.
#
# Consequences for this script:
#   - checking the association (`iw link`) is NOT enough, it stays reported;
#   - real connectivity must be tested: a ping of the gateway;
#   - the only proven recovery is the full off/on done by ES's Network menu,
#     i.e. a connman restart (S08connman).
#
# It also keeps Wi-Fi power-save off: with power-save on, the link drops idle
# SSH sessions.
#
#   sh wifi-watchdog.sh [iface] &      # for one transfer, or as a service
#
# Output in /userdata/system/logs/wifi-watchdog.log

LOG=/userdata/system/logs/wifi-watchdog.log
IFACE=${1:-wlan0}
PERIOD=20       # seconds between two checks
THRESHOLD=3     # consecutive failed gateway pings before acting (= 1 min)

mkdir -p "$(dirname "$LOG")"
exec >>"$LOG" 2>&1
echo "=== $(date '+%F %T') watching $IFACE (every ${PERIOD}s, threshold $THRESHOLD failures)"

associated() {
	iw dev "$IFACE" link 2>/dev/null | grep -q '^Connected to'
}

gateway() {
	ip -4 route show dev "$IFACE" 2>/dev/null | awk '/^default/ {print $3; exit}'
}

reachable() {
	gw=$(gateway)
	[ -n "$gw" ] || return 1
	ping -c 1 -W 2 -I "$IFACE" "$gw" >/dev/null 2>&1
}

recover() {
	# 1) the gentle way: ask connman to reconnect to the known service
	svc=$(connmanctl services 2>/dev/null | awk '{print $NF}' | grep '^wifi_' | head -1)
	if [ -n "$svc" ]; then
		echo "  $(date '+%T') connmanctl connect $svc"
		connmanctl connect "$svc" 2>&1 | sed 's/^/    /'
		sleep 8
		reachable && return 0
	fi
	# 2) the proven way: full off/on, like ES's Network menu
	echo "  $(date '+%T') still nothing: ip link down/up + connman restart"
	ip link set "$IFACE" down 2>/dev/null
	sleep 2
	ip link set "$IFACE" up 2>/dev/null
	/etc/init.d/S08connman restart >/dev/null 2>&1
	sleep 15
	reachable
}

failures=0
losses=0
while true; do
	iw dev "$IFACE" set power_save off 2>/dev/null
	if reachable; then
		[ $failures -gt 0 ] && echo "$(date '+%F %T') gateway reachable again"
		failures=0
	else
		failures=$((failures + 1))
		if associated; then
			state="associated but gateway unreachable (zombie link?)"
		else
			state="not associated"
		fi
		echo "$(date '+%F %T') $IFACE: $state - failure $failures/$THRESHOLD"
		if [ $failures -ge $THRESHOLD ]; then
			losses=$((losses + 1))
			echo "$(date '+%F %T') LOSS #$losses, recovering"
			if recover; then
				echo "  $(date '+%T') restored: $(ip -4 -br addr show "$IFACE" | awk '{print $3}')"
				failures=0
			else
				echo "  $(date '+%T') not restored, trying again next round"
			fi
		fi
	fi
	sleep "$PERIOD"
done
