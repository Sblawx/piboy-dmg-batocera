#!/bin/sh
# Run by /etc/init.d/S00bootcustom, i.e. before EmulationStation (S31).
#
# EmulationStation resolves the battery sysfs path exactly once, on its first
# query, and caches the miss forever: Platform.cpp writes "." into
# batteryStatusPath when /sys/class/power_supply is empty, and the guard is
# `if (batteryStatusPath.empty())`, so it never scans again. The gauge must
# therefore already exist when ES starts. The full daemon only comes up at S99
# (S99userservices -> custom.sh), far too late, so publish a first BAT0 here
# and let the daemon take over the live values later.
#
# This lives on /boot because it is the only writable persistent storage that
# is mounted this early: / is a squashfs under a *tmpfs* overlay (so /etc is
# wiped on every boot) and /userdata is not mounted until S11.
#
# At shutdown, S00bootcustom is also the LAST init.d script rcK runs (reverse
# order), and it runs before the final umount, so the "stop" case here is the
# right place to tell the MCU it may cut the rail (flags=0). Nothing else does
# it on this image: the vendor's piboy_power_ctrl.py is not wired up, and ES is
# not built with the PIBOY patch. Without this, "Shutdown" from the ES menu
# halts the Pi but leaves the MCU powered -> battery drains while "off". The
# daemon already handles the power-switch and low-battery paths itself; this
# closes the menu-shutdown path.

XPI=/sys/kernel/xpi_gamecon
PSDIR=/sys/class/power_supply

case "$1" in
	start)
		# Custom boot branding. Must run before S03system-splash, which
		# displays boot-logo-<fb0 virtual_size>.png with fbv. The images
		# cannot live on /userdata: that is not mounted until S11, long
		# after the logo is on screen. /boot is already mounted here, and
		# it also survives Batocera updates -- those replace the squashfs,
		# which would otherwise restore the stock logos on every upgrade.
		# Kept above the exits below so a missing gamecon node, or a kernel
		# that already populates power_supply, never skips the branding.
		if [ -d /boot/branding ]; then
		rm -f /usr/share/batocera/splash/boot-logo*.png 2>/dev/null  # first boot logo disabled (the splash video follows); to re-enable: cp -f /boot/branding/boot-logo*.png /usr/share/batocera/splash/
			cp -f /boot/branding/es-logo.png 				/usr/share/emulationstation/resources/logo.png 2>/dev/null
		fi

		modprobe xpi_gamecon 2>/dev/null
		[ -d "$XPI" ] || exit 0

		# Only shadow PSDIR if the kernel has nothing there of its own.
		[ -n "$(ls -A "$PSDIR" 2>/dev/null)" ] && exit 0
		mount -t tmpfs -o size=64k tmpfs "$PSDIR" 2>/dev/null || exit 0

		mkdir -p "$PSDIR/BAT0"
		echo Battery >"$PSDIR/BAT0/type"
		echo 1 >"$PSDIR/BAT0/present"
		echo Li-ion >"$PSDIR/BAT0/technology"

		MA=$(cat "$XPI/amps" 2>/dev/null)
		MV=$(cat "$XPI/battery" 2>/dev/null)
		# Placeholder until the daemon seeds its own gauge ~10s later. It
		# must be close, because ES latches onto whatever it reads first.
		# Same measured OCV curve and SoC-dependent pack resistance as
		# piboy-dmgcontrol.py, coarsened to segments POSIX integer
		# arithmetic can carry (worst case ~1.5 points off the real table).
		# R is solved in two passes because it depends on the very state of
		# charge being estimated - 50 mOhm full, 130 near empty.
		# The old code here used R=162mOhm against a linear 3345..4080mV
		# map: 162 was disproved back in July (it is charger overpotential,
		# not pack resistance) and the daemon was corrected, but this copy
		# was missed, so every boot showed a value ~15 points too high
		# until the daemon overwrote it.
		PCT=50
		if [ -n "$MV" ] && [ -n "$MA" ]; then
			R=80
			for _pass in 1 2; do
				OCV=$((MV - MA * R / 1000))
				if   [ "$OCV" -ge 4047 ]; then PCT=$(( 92 + (OCV - 4047) * 8 / 53 ))
				elif [ "$OCV" -ge 3901 ]; then PCT=$(( 75 + (OCV - 3901) * 17 / 146 ))
				elif [ "$OCV" -ge 3762 ]; then PCT=$(( 57 + (OCV - 3762) * 18 / 139 ))
				elif [ "$OCV" -ge 3680 ]; then PCT=$(( 44 + (OCV - 3680) * 13 / 82 ))
				elif [ "$OCV" -ge 3593 ]; then PCT=$(( 22 + (OCV - 3593) * 22 / 87 ))
				elif [ "$OCV" -ge 3489 ]; then PCT=$((  5 + (OCV - 3489) * 17 / 104 ))
				else                           PCT=$(( (OCV - 3400) * 5 / 89 ))
				fi
				[ "$PCT" -lt 0 ] && PCT=0
				[ "$PCT" -gt 100 ] && PCT=100
				if   [ "$PCT" -ge 75 ]; then R=52
				elif [ "$PCT" -ge 50 ]; then R=68
				elif [ "$PCT" -ge 25 ]; then R=90
				elif [ "$PCT" -ge 10 ]; then R=110
				else                         R=130
				fi
			done
		fi
		echo "$PCT" >"$PSDIR/BAT0/capacity"
		# Negative current means draining. (Bit 0x80 of `status` is VBus
		# and IS reliable - measured 0x46 on battery, 0xc6 plugged - but
		# the current sign is what ES cares about for the placeholder.)
		if [ "${MA:-0}" -lt 0 ] 2>/dev/null; then
			echo Discharging >"$PSDIR/BAT0/status"
		else
			echo Charging >"$PSDIR/BAT0/status"
		fi
		[ -n "$MA" ] && echo $((MA * 1000)) >"$PSDIR/BAT0/current_avg"
		[ -n "$MA" ] && echo $((MA * 1000)) >"$PSDIR/BAT0/current_now"
		[ -n "$MV" ] && echo $((MV * 1000)) >"$PSDIR/BAT0/voltage_now"
		;;
	stop)
		# Positive detection only: cut the rail solely when ES left its
		# shutdown marker. A reboot leaves /tmp/reboot.please instead (never
		# shutdown.please), and a bare `reboot`/`poweroff` from a shell leaves
		# no marker at all -- in both cases we do nothing, because wrongly
		# writing flags=0 on a reboot would power the board off instead of
		# letting it come back up. The power-switch and low-battery paths are
		# already handled by the daemon, which writes flags=0 itself. /tmp is
		# still mounted at this point (umount runs after rcK).
		# Done unconditionally, charger or not. A brief 2026-07-28
		# experiment skipped this when VBus was present, on the theory
		# that leaving the MCU alive would let the pack charge
		# overnight. It does not: the pack does not charge at ALL once
		# the Pi halts (the MCU drops the board rail when the 120Hz
		# heartbeat stops), measured twice - the console came back from
		# both a 17h night and an 8.5h afternoon on the charger having
		# *lost* open-circuit voltage. So there is nothing to gain, and
		# skipping this would only restore the pre-07-17 standby drain.
		# To actually charge, the console has to stay switched on; the
		# daemon's charge-hold mode covers the low-battery case.
		if [ -d "$XPI" ] && [ -e /tmp/shutdown.please ]; then
			sync
			echo 0 >"$XPI/flags" 2>/dev/null
		elif [ -d "$XPI" ]; then
			# Reboot (or a bare shell reboot/poweroff): 129 = display on +
			# bit 7, which opens a 60-second window where the MCU will not
			# cut the Pi while its heartbeat is missing. Experimental Pi's own
			# image does exactly this on reboot (firmware 1.0.2+); without it
			# a slow boot, e.g. right after a Batocera update, can be powered
			# off halfway. For a bare poweroff it only delays the power cut.
			echo 129 >"$XPI/flags" 2>/dev/null
		fi
		;;
esac

exit 0
