# Windows games on the PiBoy (Pi 4): box64 + Wine

This is optional and only for the Pi 4 (a Pi 3 is too slow). It runs x86
Windows games through [box64](https://github.com/ptitSeb/box64) (x86_64 to ARM64
translation) and Wine. StarCraft 1.16.1 is the reference game here: it runs in
its native 640x480, with the analog stick as the mouse and StarCraft shortcuts
on the face buttons. (LAN multiplayer against a PC should work, since Wine
uses real network sockets, but it has not been tested yet.)

You need to provide the game yourself (an installed StarCraft 1.16.1 folder).
Nothing here downloads or ships any game.

## Setup

Everything goes under `/userdata/system`, so it survives Batocera updates.

1. **box64**: an aarch64 build for the Pi 4, as `/userdata/system/box64/box64`.
2. **x86_64 runtime libraries** box64 needs for Wine, in
   `/userdata/system/box64/x86lib/` (at least `libgcc_s.so.1` from a Debian
   amd64 `libgcc-s1` package).
3. **Wine**: an **amd64-wow64** build (pure 64-bit Wine that runs 32-bit
   programs through the new WoW64 mode, so no box86 and no armhf libraries are
   needed), for example the ones published by Kron4ek. Unpack it and point
   `/userdata/system/wine/current` at it.
4. **Wine prefix**: `WINEPREFIX=/userdata/system/wine/prefix`. The first
   `winebox.sh wineboot -u` is slow under box64; give it several minutes.
5. **The game**: copy your installed StarCraft folder to
   `/userdata/roms/windows/StarCraft/`.
6. Re-run `install.sh`: it detects box64 + Wine and adds **StarCraft** to Ports.

## Controls in StarCraft

| PiBoy | StarCraft |
|---|---|
| Stick | mouse |
| A / B | left / right click |
| D-pad | arrow keys (scroll the map) |
| X | Attack (A) |
| Y | Stop (S) |
| C | Hold (H) |
| Z | Enter |
| L / R (held) | Shift / Ctrl |
| SELECT + START | quit |

## Files

| File | What |
|---|---|
| `winebox.sh` | runs `wine` through box64 with the right environment and dynarec tuning |
| `wine-quit.sh` | ends the whole Wine session cleanly so ES comes back |
| `starcraft.sh` | the launcher: mouse daemon, CPU governor, 640x480 Wine desktop |
| `ports/StarCraft.sh` | the Ports entry |

The first launch of a session takes 30 to 60 s while box64 translates the
game (there is no persistent translation cache). That is expected.

Other Windows games can be launched the same way: copy `starcraft.sh`, change
the folder and the executable, and add a Ports entry.
