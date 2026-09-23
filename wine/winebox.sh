#!/bin/sh
# Runs Wine (x86_64, new WoW64) through box64 on the PiBoy's aarch64 Batocera.
# Paths match wine/README.md.
export BOX64=/userdata/system/box64/box64
export WDIR=/userdata/system/wine/current
export BOX64_LD_LIBRARY_PATH="/userdata/system/box64/x86lib:$WDIR/lib/wine/x86_64-unix:$WDIR/lib:$WDIR/lib64:/usr/lib:/lib"
export BOX64_PATH="$WDIR/bin"
export WINEPREFIX=/userdata/system/wine/prefix
export WINELOADER="$WDIR/bin/wine"
export WINEDLLOVERRIDES="${WINEDLLOVERRIDES:-mscoree=d;mshtml=d}"
export WINEDEBUG="${WINEDEBUG:--all}"     # Wine logging is VERY expensive under box64
export DISPLAY="${DISPLAY:-:0.0}"
export PATH="$WDIR/bin:$PATH"

# box64 dynarec tuning (typically 10-25 % faster)
export BOX64_LOG=0
export BOX64_DYNAREC=1
export BOX64_DYNAREC_BIGBLOCK=2
export BOX64_DYNAREC_CALLRET=1
export BOX64_DYNAREC_FASTNAN=1
export BOX64_DYNAREC_FASTROUND=1

exec "$BOX64" "$WDIR/bin/wine" "$@"
