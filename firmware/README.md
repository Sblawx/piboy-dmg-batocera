# PiBoy DMG MCU firmware

The PiBoy's microcontroller (buttons, power, battery, fan, LED, screen power)
runs its own firmware. These are the **last releases published by
Experimental Pi**, mirrored here from their download server archive
(https://archive.org/details/EXPPI) since the company has shut down.

**You probably don't need this.** The installer prints your firmware version
(`cat /sys/kernel/xpi_gamecon/version` gives it too: 262 = 0x106 = 1.0.6). Only
update if yours is older than the latest one for your model.

## Which file?

Experimental Pi made two DMG hardware revisions. Their updater asks the
microcontroller which one it is, then flashes the matching file:

| Unit reports itself as | Latest firmware | File |
|---|---|---|
| Piboy DMG | 1.0.6 | `PIBOYDMG32K.106.bin` |
| Piboy DMGx | 1.0.7 | `PIBOYDMGx32K.bin` |

The images are encrypted for the PiBoy's bootloader, so the file itself does
not tell which model it is for. `loader.py` below does **not** check the model:
if you are not sure which revision you have, use Experimental Pi's Windows
utility from the archive.org link above, which detects it.

What each release changed: [changes.txt](changes.txt). 1.0.6 notably fixed
reboot/shutdown issues and the joystick calibration.

## Flashing with loader.py (Linux, macOS, Windows)

`loader.py` is Experimental Pi's own command-line updater (MIT licence, see
[LICENSE-loader.txt](LICENSE-loader.txt)). It needs Python 3 and `pyserial`.

1. Connect the PiBoy to the computer over USB. Its microcontroller shows up as
   a USB serial port (Atmel, USB ID `03eb:2404`): `/dev/ttyACM0` on Linux,
   a `COMx` port on Windows (Windows may need the Atmel CDC driver shipped with
   the official utility).
2. `loader.py` always flashes a file named `PIBOYDMG32K.bin` from the current
   folder. Copy **the file for your model** under that name:

   ```sh
   pip install pyserial
   cp PIBOYDMG32K.106.bin PIBOYDMG32K.bin      # Piboy DMG
   # or: cp PIBOYDMGx32K.bin PIBOYDMG32K.bin   # Piboy DMGx
   python3 loader.py /dev/ttyACM0               # or COM4 etc. on Windows
   ```

3. Wait for `Done`; the microcontroller resets by itself.

Flashing the wrong model's firmware, or interrupting a flash, can leave the
console unusable. Do it on a charged battery and at your own risk.

## Checksums (SHA-256)

```
69f953f8e965676de04ed0e749cc24812bc7b520f8d987192720b8ab20b7d0c6  PIBOYDMG32K.106.bin
55d00adc5729436cf13b239f0a8011b1305947e142f3ad7e355f2d652c197f21  PIBOYDMGx32K.bin
2b2f516cc2c86e8a165bc05d1d55d1a36d3c1b541db73c4e9d0ea70d64b9e176  loader.py   (Unix line endings)
```

## Rights

The firmware images belong to Experimental Pi. They are mirrored here only to
keep existing consoles maintainable now that the official download server is
gone. If you hold the rights and want them removed, open an issue.
