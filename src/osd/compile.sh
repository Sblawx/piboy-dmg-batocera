#!/bin/sh
set -e
SCANNER=$(command -v wayland-scanner)
WLPROTO_DIR=/usr/share/wayland-protocols
mkdir -p build gen
for h in stb_image.h stb_truetype.h; do
  [ -f "gen/$h" ] || curl -fsSL -o "gen/$h" "https://raw.githubusercontent.com/nothings/stb/master/$h"
done
"$SCANNER" client-header "$WLPROTO_DIR/stable/xdg-shell/xdg-shell.xml" gen/xdg-shell-client-protocol.h
"$SCANNER" private-code  "$WLPROTO_DIR/stable/xdg-shell/xdg-shell.xml" gen/xdg-shell-protocol.c
[ -f gen/wlr-layer-shell-unstable-v1.xml ] || curl -fsSL -o gen/wlr-layer-shell-unstable-v1.xml "https://gitlab.freedesktop.org/wlroots/wlr-protocols/-/raw/master/unstable/wlr-layer-shell-unstable-v1.xml"
"$SCANNER" client-header gen/wlr-layer-shell-unstable-v1.xml gen/wlr-layer-shell-unstable-v1-client-protocol.h
"$SCANNER" private-code  gen/wlr-layer-shell-unstable-v1.xml gen/wlr-layer-shell-unstable-v1-protocol.c
CF="-O2 -Wall -Igen $(pkg-config --cflags wayland-client)"
LB="$(pkg-config --libs wayland-client) -lm -pthread"
gcc $CF -o build/piboy-osd src/piboy-osd.c gen/xdg-shell-protocol.c gen/wlr-layer-shell-unstable-v1-protocol.c $LB
strip build/piboy-osd
gcc $CF -o build/piboy-settings src/piboy-settings.c gen/xdg-shell-protocol.c gen/wlr-layer-shell-unstable-v1-protocol.c $LB
strip build/piboy-settings
echo "BUILD OK"; readelf -V build/piboy-osd | grep -oE 'GLIBC_[0-9]+\.[0-9]+' | sort -uV | tail -1
