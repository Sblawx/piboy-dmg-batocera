#!/usr/bin/env python3
"""Listens for netplay announcements on the LAN and describes the first usable one.

Output on stdout, one tab-separated line:
    system <TAB> rom <TAB> core <TAB> ip <TAB> port
Nothing on stdout and a non-zero exit code if nothing can be joined; in that
case the reason is also shown on screen through EmulationStation.

Refusals are explicit rather than silent: a core missing on this console (a
Pi 3 image ships fewer systems than a Pi 4 one) must give an understandable
message, not an obscure RetroArch failure.
"""
import json
import os
import socket
import sys
import time
import urllib.request

ANNOUNCE_PORT = 55436
LOG = '/userdata/system/piboy-netplay.log'
SLOW_CORES = '/userdata/system/piboy-netplay-cores.conf'


def log(message):
    try:
        with open(LOG, 'a', encoding='utf-8') as f:
            f.write('[find] %s %s\n' % (time.strftime('%m-%d %H:%M:%S'), message))
    except OSError:
        pass


def notify(message):
    """On-screen message through EmulationStation's local API."""
    try:
        req = urllib.request.Request('http://127.0.0.1:1234/notify',
                                     data=('Netplay: ' + message).encode('utf-8'),
                                     method='POST')
        urllib.request.urlopen(req, timeout=2).read()
    except Exception:
        pass


def slow_cores():
    """Cores known to be too slow on a given board (hand-written file).

    Whether a core exists can be detected; whether it is fast enough cannot.
    That knowledge has to be written down by a human after playing.
    """
    table = {}
    try:
        with open(SLOW_CORES, encoding='utf-8') as f:
            for line in f:
                line = line.split('#')[0].strip()
                if not line or ':' not in line:
                    continue
                board, cores = line.split(':', 1)
                table[board.strip()] = [c.strip() for c in cores.split(',') if c.strip()]
    except OSError:
        pass
    return table


def my_board():
    try:
        with open('/boot/boot/batocera.board', encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return 'unknown'


def main():
    wait = float(sys.argv[1]) if len(sys.argv) > 1 else 10.0
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        rx.bind(('', ANNOUNCE_PORT))
    except OSError as e:
        log('cannot listen on %d: %s' % (ANNOUNCE_PORT, e))
        notify('cannot listen for games (port %d busy)' % ANNOUNCE_PORT)
        return 1
    rx.settimeout(1.0)

    me = os.uname()[1]
    board = my_board()
    slow = slow_cores().get(board, [])
    log('looking for a game for %.0fs (board %s)' % (wait, board))
    end = time.monotonic() + wait
    last_refusal = None

    while time.monotonic() < end:
        try:
            data, _ = rx.recvfrom(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        try:
            room = json.loads(data.decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            continue
        if room.get('console') == me:
            continue            # our own announcement

        rom = room.get('rom')
        core = room.get('core') or room.get('coeur')
        system = room.get('system') or room.get('systeme')
        game = room.get('game') or room.get('jeu') or ''
        if not rom or not core:
            continue
        if not os.path.exists(rom):
            last_refusal = '"%s" is not on this console (same file, same path needed)' % game
            log('skipped: %s' % last_refusal)
            continue
        if not os.path.exists('/usr/lib/libretro/%s_libretro.so' % core):
            last_refusal = 'core %s is not available on this console' % core
            log('REFUSED: %s' % last_refusal)
            continue
        if core in slow:
            log('warning: %s is known to be too slow on %s' % (core, board))
            notify('%s may be too slow on this console' % core)

        log('joining: %s / %s / core=%s / %s:%s'
            % (system, game, core, room.get('ip'), room.get('port')))
        print('%s\t%s\t%s\t%s\t%s' % (system, rom, core, room.get('ip'), room.get('port')))
        return 0

    if last_refusal:
        notify(last_refusal)
    else:
        log('no game found')
        notify('no hosted game found on the local network')
    return 1


if __name__ == '__main__':
    sys.exit(main())
