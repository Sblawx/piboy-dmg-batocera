#!/usr/bin/env python3
"""Cross-compiles piboy-osd and piboy-settings for aarch64 Batocera, no Docker.

Needs only Python 3 and a clang with lld (any LLVM install, or the one inside
the Android NDK). Downloads a handful of Debian bookworm arm64 packages (glibc,
kernel headers, libwayland) into ./sysroot, then links against them. glibc 2.36
binaries run fine on Batocera 43 (glibc 2.40).

    python3 build-cross.py                          # clang found on PATH
    python3 build-cross.py --clang /path/to/clang   # explicit compiler

Output: build/piboy-osd, build/piboy-settings
"""
import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request

POOL = 'https://deb.debian.org/debian/pool/main/'
PACKAGES = [
    ('g/glibc/', r'libc6-dev_2\.36-9\+deb12u\d+_arm64\.deb'),
    ('g/glibc/', r'libc6_2\.36-9\+deb12u\d+_arm64\.deb'),
    ('l/linux/', r'linux-libc-dev_6\.1\.\d+-1_arm64\.deb'),
    ('w/wayland/', r'libwayland-dev_1\.21\.0-1_arm64\.deb'),
    ('w/wayland/', r'libwayland-client0_1\.21\.0-1_arm64\.deb'),
    ('g/gcc-12/', r'libgcc-12-dev_12\.2\.0-14\+deb12u\d+_arm64\.deb'),
    ('g/gcc-12/', r'libgcc-s1_12\.2\.0-14\+deb12u\d+_arm64\.deb'),
]
HERE = os.path.dirname(os.path.abspath(__file__))
SYSROOT = os.path.join(HERE, 'sysroot')


def latest(directory, pattern):
    listing = urllib.request.urlopen(POOL + directory).read().decode('utf-8', 'replace')
    names = sorted(set(re.findall('href="(%s)"' % pattern, listing)),
                   key=lambda n: [int(x) for x in re.findall(r'\d+', n)])
    if not names:
        sys.exit('package not found in %s: %s' % (directory, pattern))
    return names[-1]


def ar_members(data):
    assert data[:8] == b'!<arch>\n', 'not a .deb'
    i = 8
    while i < len(data):
        header = data[i:i + 60]
        name = header[:16].decode().strip().rstrip('/')
        size = int(header[48:58])
        i += 60
        yield name, data[i:i + size]
        i += size + (size & 1)


def extract(deb_bytes, links):
    for name, data in ar_members(deb_bytes):
        if not name.startswith('data.tar'):
            continue
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            for m in t.getmembers():
                rel = m.name.lstrip('./')
                dst = os.path.join(SYSROOT, rel)
                if m.isdir():
                    os.makedirs(dst, exist_ok=True)
                elif m.issym() or m.islnk():
                    links.append((rel, m.linkname, m.islnk()))
                elif m.isfile():
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    with open(dst, 'wb') as f:
                        f.write(t.extractfile(m).read())


def resolve_links(links):
    # Symlinks are materialised as copies so this also works on Windows.
    for _ in range(3):
        for rel, target, hard in links:
            dst = os.path.join(SYSROOT, rel)
            if os.path.exists(dst):
                continue
            if hard or target.startswith('/'):
                src = target.lstrip('/')
            else:
                src = os.path.normpath(os.path.join(os.path.dirname(rel), target))
            src = os.path.join(SYSROOT, src)
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)


def make_sysroot():
    if os.path.exists(os.path.join(SYSROOT, 'lib', 'aarch64-linux-gnu', 'libgcc_s.so.1')):
        return
    links = []
    for directory, pattern in PACKAGES:
        name = latest(directory, pattern)
        print('downloading', name)
        extract(urllib.request.urlopen(POOL + directory + name).read(), links)
    resolve_links(links)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--clang', default=shutil.which('clang') or 'clang')
    args = ap.parse_args()
    make_sysroot()
    clang = args.clang
    strip = os.path.join(os.path.dirname(clang), 'llvm-strip' + ('.exe' if clang.endswith('.exe') else ''))
    os.makedirs(os.path.join(HERE, 'build'), exist_ok=True)
    # -ffile-prefix-map keeps the build machine's paths out of the binaries
    # (stb's assert() messages embed __FILE__).
    common = ['--target=aarch64-linux-gnu', '--sysroot=' + SYSROOT, '-fuse-ld=lld', '-O2', '-Wall',
              '-ffile-prefix-map=' + HERE + '=src/osd',
              '-Wno-unused-parameter', '-I' + os.path.join(HERE, 'gen'),
              '-L' + os.path.join(SYSROOT, 'lib', 'aarch64-linux-gnu')]
    protocols = [os.path.join(HERE, 'gen', 'xdg-shell-protocol.c'),
                 os.path.join(HERE, 'gen', 'wlr-layer-shell-unstable-v1-protocol.c')]
    for prog in ('piboy-osd', 'piboy-settings'):
        out = os.path.join(HERE, 'build', prog)
        cmd = [clang] + common + ['-o', out, os.path.join(HERE, prog + '.c')] + protocols + \
              ['-lwayland-client', '-lm', '-pthread']
        print(' '.join(os.path.basename(c) if i == 0 else c for i, c in enumerate(cmd[:3])), '...', prog)
        subprocess.check_call(cmd)
        if os.path.exists(strip):
            subprocess.check_call([strip, out])
    print('done: build/piboy-osd, build/piboy-settings (copy them to /userdata/system/piboy-osd/)')


if __name__ == '__main__':
    main()
