# xpi-user: PiBoy XRS (and DMG) controls on stock Batocera, without a kernel module

`xpi_user.py` talks to the PiBoy's controller chip over GPIO 26/27 exactly like
Experimental Pi's `xpi_gamecon` kernel driver (GPL, Nathan Scherdin), but as a normal
program. No kernel module means nothing to rebuild when Batocera updates.

It provides the gamepad ("Experimental Pi Controller": two sticks, d-pad, 12 buttons),
the volume wheel, fan control, the power switch (clean shutdown), and the battery
values in `/run/xpi_gamecon` (same file names as the kernel driver's sysfs).

> **Status: tested on one PiBoy XRS** (controller firmware 1.0.7): all buttons, both
> sticks and their clicks, the d-pad, the menu button (volume/brightness mode, long
> press to shut down) and the automatic start at boot work. The fan speed scale was
> fixed afterwards and awaits confirmation. Not yet tried on a DMG. Please run step 3
> first.

Raspberry Pi 4 (or 3) only. Tested target: Batocera 43.1 (`bcm2711`).

## 1. Prepare a spare SD card, on your PC

1. Flash `batocera-bcm2711-43.1` and open the **BATOCERA** partition (FAT, any OS).
   Edit its files with a plain text editor (Notepad, TextEdit in plain text mode, nano...).
2. Screen, in `config.txt`:
   1. near the end, find these two lines:

      ```
      # Enable DRM VC4 V3D driver
      dtoverlay=vc4-kms-v3d
      ```

      and put a `#` at the start of the second one, so it reads
      `#dtoverlay=vc4-kms-v3d`. If you leave it on, you get the Batocera splash,
      then a black screen;
   2. paste the whole content of `config-piboy-pi4.txt` at the very end of the file,
      after the last `[all]` line. Without it the internal screen stays black.
3. Wi-Fi, to reach the console over SSH (the buttons do nothing until the driver
   runs, so the menu cannot be used for this): add these three lines at the end
   of `batocera-boot.conf`, with your network's name and password:

   ```
   wifi.enabled=1
   wifi.ssid=YourNetworkName
   wifi.key=YourPassword
   ```

   Batocera only reads them on the **very first boot** of a freshly flashed card.
   If the card has already been started once, reflash it, or plug in a USB keyboard
   to set up Wi-Fi from the menu.
4. Copy this folder to the root of the partition, named `xpi-user`, so that the file
   is at `xpi-user/xpi_user.py` (on the console: `/boot/xpi-user/xpi_user.py`).

## 2. Boot the XRS

Nothing starts automatically yet: this is stock Batocera. Log in from your PC:

```
ssh root@batocera.local        (password: linux)
```

If the console switches itself off before you get there, stop and tell me: it would
mean the chip wants a heartbeat from the very start.

## 3. Test (nothing is installed)

```
python3 /boot/xpi-user/xpi_user.py --test
```

It prints a line each time something changes, and a statistics line every 2 s.
Press every button, move both sticks and the d-pad, turn the volume wheel.
Stop with **Ctrl-C**, then run `poweroff` **within 60 seconds**: once the driver
stops, the chip only keeps the Pi powered for about a minute. When the Pi has
halted, flip the power switch off (nothing tells the chip to switch itself off yet).

Please send back the whole output. The important parts: are the button names right,
and how many CRC errors (a few per 10,000 frames is normal)?

## 4. Install (starts at every boot)

Only if step 3 looked right:

```
python3 /boot/xpi-user/xpi_user.py --enable
reboot
```

Then in EmulationStation: configure the controller ("Experimental Pi Controller").
Try a game, the volume wheel, a reboot from the menu, a shutdown from the menu, and
the power switch. Please send back:

```
cat /tmp/xpi-user.log
top -b -n 1 | head -15
```

(the second one while a game runs, to see how much CPU `python3` uses).

## Undo

```
python3 /boot/xpi-user/xpi_user.py --disable
```

or, from a PC, delete `boot-custom.sh` (or `xpi-user/enabled`) on the BATOCERA
partition.

## How it behaves

- Polls the chip 100 times a second (`--hz` to change it). The chip also treats this
  as the Pi's heartbeat. It costs about a quarter of one CPU core.
- Fan: off below 60 °C, then about 30, 45, 70 and 90 % at 60, 67, 73 and 79 °C, full
  from 80 °C.
- Battery, temperature and fan are not shown in EmulationStation: this is only the
  driver.
- When it stops, it sends `flags=129` (display on + 60 s grace, what the vendor image
  sends on reboot), unless the system is powering off, in which case it sends
  `flags=0` so the chip switches itself off instead of draining the battery.
- Power switch off for 1.5 s, or battery under 3.25 V while discharging for a minute:
  stops EmulationStation, syncs, `flags=0`, `poweroff`. It never acts on the switch
  bit before having seen it "on" once.
- Refuses to run if the `xpi_gamecon` kernel module is loaded (both would drive the
  same two pins). If it gets no valid frame in 5 s at boot, it disables itself.
- `--model dmg` drives a PiBoy DMG instead (12-byte frame).
