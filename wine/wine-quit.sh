#!/bin/sh
# Cleanly ends the whole Wine session so the Ports launcher returns and
# EmulationStation takes over again. Killing the game alone leaves explorer,
# wineserver and Wine's background services alive, and ES stays on "loading".
export WDIR=/userdata/system/wine/current
export BOX64_LD_LIBRARY_PATH="/userdata/system/box64/x86lib:$WDIR/lib/wine/x86_64-unix:$WDIR/lib:$WDIR/lib64:/usr/lib:/lib"
export WINEPREFIX=/userdata/system/wine/prefix
# Wine's own mechanism: ends every process of the session, then the server.
/userdata/system/box64/box64 "$WDIR/bin/wineserver" -k 2>/dev/null
# Safety net in the background if anything lingers.
( sleep 3; pkill -KILL -f 'x86_64-unix/wine'; pkill -KILL -f 'bin/wineserver' ) >/dev/null 2>&1 &
