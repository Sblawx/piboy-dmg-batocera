#!/bin/sh
# xpi-user boot hook - installed as /boot/boot-custom.sh by "xpi_user.py --enable".
# Batocera runs it through /etc/init.d/S00bootcustom: "start" first thing at boot,
# "stop" as the very last shutdown script (before the final umount).
#
# start: runs the user-space PiBoy driver and restarts it if it ever dies (its
#        polling is the MCU's heartbeat). Exit codes 3 (no valid frame) and 4
#        (the xpi_gamecon kernel module is loaded) stop the retries: the driver
#        has then disabled itself.
# stop:  tells the MCU how the Pi is going away. flags=0 only for a real
#        poweroff (ES's /tmp/shutdown.please, or the driver's own power-switch /
#        low-battery path); anything else gets 129 = display on + 60 s grace,
#        so a reboot is not cut halfway. Same rule as the PiBoy DMG layer.

D=/boot/xpi-user
RUN=/run/xpi_gamecon

case "$1" in
	start)
		[ -f "$D/enabled" ] || exit 0
		MODEL=$(cat "$D/model" 2>/dev/null)
		[ -n "$MODEL" ] || MODEL=xrs
		(
			while [ -f "$D/enabled" ] && [ ! -e "$RUN/stop" ]; do
				python3 "$D/xpi_user.py" --daemon --model "$MODEL" >>/tmp/xpi-user.log 2>&1
				rc=$?
				if [ $rc -eq 3 ] || [ $rc -eq 4 ]; then
					break
				fi
				sleep 1
			done
		) </dev/null >/dev/null 2>&1 &
		;;
	stop)
		[ -d "$RUN" ] || exit 0
		touch "$RUN/stop"
		if [ -e /tmp/shutdown.please ] || [ -e "$RUN/poweroff" ]; then
			sync
			echo 0 >"$RUN/flags"
		else
			echo 129 >"$RUN/flags"
		fi
		# the driver reads the file every 0.1 s and sends it in the next frames
		sleep 1
		;;
esac

exit 0
