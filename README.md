# flexitac-live

A live viewer for [FlexiTac](https://flexitac.github.io/) tactile sensors that actually behaves on a laptop. It reads the sensor over serial and shows the real per-taxel force values on a grid, and it holds latency flat instead of letting the heatmap fall behind your finger.

I picked up a FlexiTac sensor and wanted to watch it live on my laptop. The stock host script assumes Linux and a fast machine: the port is hardcoded to `/dev/ttyUSB0`, and on Windows the heatmap kept sliding further behind my finger the longer it ran. It also only ever showed an auto-scaled blob, never actual numbers. So I wrote my own host-side reader that runs on Windows, macOS, and Linux, shows absolute force per taxel, and stays responsive no matter how slow the host is.

This is host-side only. It speaks the sensor's documented serial format but ships no firmware, so you still flash the official firmware from the upstream [FlexiTac repo](https://github.com/FlexiTac/FlexiTac_Hardware_Repo). It is an independent tool and is not affiliated with the FlexiTac authors.

## What it does

- Auto-detects the serial port (FTDI, CH340, or CP210x), so you rarely need to pass `--port`, and it runs on Windows and macOS, not just Linux.
- Keeps latency bounded. The reader always jumps to the newest complete frame and throws away the backlog, so lag never piles up on a slow host. This was the fix that actually mattered.
- Shows absolute force per taxel, as raw minus baseline in ADC counts, instead of normalizing every frame by its own max. The numbers mean something.
- Draws a readable grid: a border around every taxel, row and column indices, the value written in each cell, and a colorbar so you can read a color back as an approximate number.
- Self-heals the baseline. Idle taxels slowly re-zero themselves so drift disappears, while a taxel under pressure freezes so a long press does not quietly fade to nothing.
- Reports `sumF`, the total force over the whole contact patch, which keeps rising with grip force even when a single taxel saturates and folds back.
- Has a raw diagnostic view (`d`) that shows unprocessed ADC values, handy for checking wiring and sensor behavior.
- Works with the 16x32 sensor and the 32x32 dev board through `--rows` and `--cols`.

## Install and run

```bash
pip install -r requirements.txt               # numpy, pyserial, opencv-python

python flexitac_live.py                        # auto-detect the port
python flexitac_live.py --port COM3            # or /dev/ttyUSB0, /dev/tty.usbserial-*
python flexitac_live.py --rows 32 --cols 32    # 32x32 dev board
python flexitac_live.py --list                 # list serial ports and exit
python flexitac_live.py --vmax 80 --scale 44 --alpha 0.6
```

You need firmware on the board first. Flash the upstream [FlexiTac firmware](https://github.com/FlexiTac/FlexiTac_Hardware_Repo) (`32_16_fast.ino`); it is the authors' work and I deliberately do not redistribute it here. The wire format the tool reads is a `0xAA 0x55` header followed by `rows * cols` bytes of 8-bit data at 2,000,000 baud.

Keys in the window:

```
q / Esc   quit
r         re-zero the baseline
n         toggle the per-taxel numbers
d         switch between the force and raw views
+ / -     change the color range (vmax)
[ / ]     change the temporal smoothing (alpha)
```

## How it works

**Keeping latency flat.** The board streams about 100 frames a second. If the host renders slower than that, the unread bytes pile up in the OS serial buffer, and because that buffer is first in, first out, a plain `read()` hands you the oldest frame first. You fall further behind every second and it never recovers. flexitac-live treats the buffer as latest-wins instead: every tick it reads everything waiting, finds the last complete frame, and drops the rest, so the delay stops growing no matter how slow the host is. A separate reader thread keeps a slow render from ever back-pressuring the serial read.

On Windows there is one more fixed delay. FTDI and CH340 USB-serial bridges hold bytes and only flush them every latency-timer interval, which defaults to 16 ms. Drop it to 1 ms in Device Manager, under the COM port's advanced settings, or run the bundled `set_ftdi_latency.cmd`. Whatever lag is left after that (roughly 40 to 80 ms) is the Velostat material's own response and recovery time, which is physical and cannot be removed in software.

**The self-healing baseline.** The baseline is the per-taxel "no contact" zero that gets subtracted from every reading. Rather than freezing it at startup, each idle taxel slowly follows its own current reading, so thermal drift and creep re-zero themselves over time. Any taxel above the contact threshold is frozen instead, so a sustained press keeps its full value rather than being subtracted away. Press `r` to re-zero everything, and do it while the pad is untouched, or your press becomes part of the zero.

**Why a single taxel folds back under hard pressure.** Press hard enough on one taxel and its value climbs, saturates, and then drops while you keep pressing harder. That comes from the resistive material and the readout, before the ADC, so no software can recover the lost information. Velostat loses most of its sensitivity above a few newtons, so the curve flattens and noise takes over, and as the force grows the contact patch spreads and shares load with neighboring taxels, so the pressure right under the center can fall even as the total rises. That is why the status line trusts `sumF`, the total over the contact patch, which stays honest with grip force even when the single-taxel peak folds back. If you need one taxel to stay monotonic too, that is a hardware fix: add a silicone or EVA layer to spread the force and keep the sensor in its sensitive range.

## Credits

The FlexiTac sensor, hardware, and firmware are by Binghao Huang and Yunzhu Li at Columbia University. Project site: <https://flexitac.github.io/>. Hardware and firmware: <https://github.com/FlexiTac/FlexiTac_Hardware_Repo>. Official Python package: <https://github.com/WT-MM/PyFlexiTac>. This viewer is an independent host-side tool and is not endorsed by the FlexiTac authors.

## License

The code in this repository is [MIT](LICENSE). The FlexiTac firmware and hardware are licensed separately by their authors under CC BY-NC 4.0 and are not included here.
