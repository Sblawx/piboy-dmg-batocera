#!/usr/bin/env python3
"""Announces on the LAN the netplay game hosted by this console.

Why a daemon instead of RetroArch's own LAN discovery: that one exists
("Refresh Netplay LAN List"), but to join it has to find the game in its
playlists, and Batocera generates none (no .lpl files), so the client would not
know what to load. Here we announce the EXACT ROM path and the core, and the
client relaunches through emulatorlauncher, which keeps the whole Batocera
integration: .7z extraction, pad mapping, shaders, aspect ratio.

Works however the game was opened, including EmulationStation's native menu
(long press A on a game -> netplay -> host).
"""
import glob
import json
import os
import re
import socket
import sys
import time

ANNOUNCE_PORT = 55436          # 55435 is the netplay port itself
INTERVAL = 1.0
LOG = '/userdata/system/piboy-netplay.log'


def log(message):
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write('[announce] %s %s\n' % (time.strftime('%m-%d %H:%M:%S'), message))
    except OSError:
        pass


def hosting_retroarch():
    """Command line of the retroarch started with --host, else None."""
    for path in glob.glob('/proc/[0-9]*/cmdline'):
        try:
            with open(path, 'rb') as f:
                raw = f.read()
        except OSError:
            continue            # the process went away meanwhile
        args = [a.decode('utf-8', 'replace') for a in raw.split(b'\0') if a]
        if not args or 'retroarch' not in os.path.basename(args[0]):
            continue
        if '--host' in args:
            return args
    return None


def describe(args):
    """Extracts core, port, rom and system from the command line."""
    core = None
    port = 55435
    for i, a in enumerate(args):
        if a == '-L' and i + 1 < len(args):
            core = re.sub(r'_libretro\.so$', '', os.path.basename(args[i + 1]))
        elif a == '--port' and i + 1 < len(args):
            try:
                port = int(args[i + 1])
            except ValueError:
                pass
    # The ROM is the last argument naming an existing file under roms/.
    rom = None
    for a in reversed(args):
        if a.startswith('/') and '/roms/' in a and os.path.exists(a):
            rom = a
            break
    system = None
    if rom:
        m = re.search(r'/roms/([^/]+)/', rom)
        if m:
            system = m.group(1)
    return core, port, rom, system


def my_address():
    """Local address used to reach the network (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def main():
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    console = os.uname()[1]
    current = None

    log('daemon started (port %d)' % ANNOUNCE_PORT)
    while True:
        args = hosting_retroarch()
        if args is None:
            if current is not None:
                log('game closed')
                current = None
            time.sleep(INTERVAL)
            continue

        core, port, rom, system = describe(args)
        ip = my_address()
        if not (core and rom and system and ip):
            time.sleep(INTERVAL)
            continue

        game = os.path.splitext(os.path.basename(rom))[0]
        # English keys, plus the older French ones so a console still running
        # the previous version of the finder can join too.
        room = {
            'console': console, 'ip': ip, 'port': port,
            'system': system, 'rom': rom, 'core': core, 'game': game,
            'systeme': system, 'coeur': core, 'jeu': game,
        }
        signature = (ip, port, rom, core)
        if signature != current:
            log('hosting: %s / %s / core=%s / %s:%d' % (system, game, core, ip, port))
            current = signature

        try:
            sender.sendto(json.dumps(room).encode('utf-8'), ('255.255.255.255', ANNOUNCE_PORT))
        except OSError as e:
            log('cannot broadcast: %s' % e)
        time.sleep(INTERVAL)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
