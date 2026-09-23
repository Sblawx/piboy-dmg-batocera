#!/bin/sh
# Builds m8c 2.2.3 for Batocera 42 aarch64 (glibc 2.40).
# Runs inside arm64v8/debian:bookworm (glibc 2.36 -> forward compatible).
# Links against the *device's own* libSDL3.so.0 (3.2.18) for an exact ABI match.
set -e

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    gcc libc6-dev make pkg-config libserialport-dev findutils binutils

# SDL3: upstream 3.2.18 headers + the device's runtime .so as link target
mkdir -p /opt/sdl3/lib /opt/pc
cp -a /work/SDL3-3.2.18/include /opt/sdl3/
ln -sf /work/sysroot/libSDL3.so.0.2.18 /opt/sdl3/lib/libSDL3.so

cat > /opt/pc/sdl3.pc <<'EOF'
prefix=/opt/sdl3
Name: sdl3
Description: SDL3 (Batocera 42 runtime, headers 3.2.18)
Version: 3.2.18
Libs: -L${prefix}/lib -lSDL3
Cflags: -I${prefix}/include
EOF

cd /work/m8c-2.2.3
make clean >/dev/null 2>&1 || true
PKG_CONFIG_PATH=/opt/pc make -j"$(nproc)"
strip m8c

cd /work
gcc -O2 -Wall -o padinfo padinfo.c $(PKG_CONFIG_PATH=/opt/pc pkg-config --cflags --libs sdl3)
strip padinfo
cd /work/m8c-2.2.3

echo "=== result ==="
file m8c 2>/dev/null || true
ls -l m8c
echo "=== NEEDED ==="
readelf -d m8c | grep -E 'NEEDED|RUNPATH'
echo "=== glibc symbol versions required ==="
readelf -V m8c | grep -oE 'GLIBC_[0-9]+\.[0-9]+' | sort -u -V
