# flexitac-live

A cross-platform, low-latency live viewer for [FlexiTac](https://flexitac.github.io/) tactile sensors.

I picked up a FlexiTac sensor and wanted to watch it on my laptop. The stock host script works, but it assumes Linux and a fast machine — on Windows the heatmap kept sliding further behind my finger the longer it ran, and it only ever showed a relative "blob" rather than actual numbers. This is what I ended up building: a portable viewer (**Windows/macOS/Linux**) that shows **real per-taxel force values**, draws a proper labelled grid + colorbar, and **keeps latency from piling up**.

It talks to the board over the sensor's documented serial format — it does **not** reuse or ship any firmware, so you still flash the official firmware from the FlexiTac repo. Independent host-side tool, not affiliated with or endorsed by the FlexiTac authors. See [Credits](#credits).

<!-- Add a screenshot/GIF here: e.g. docs/demo.gif -->
<!-- ![demo](docs/demo.gif) -->

## Why

The stock host script ([`fast_32_16.py`](https://github.com/FlexiTac/FlexiTac_Hardware_Repo/blob/main/fast_32_16.py)) works on the authors' Linux setup, but has a few rough edges if you run it elsewhere:

1. **Hardcoded `/dev/ttyUSB0`** — does not run on Windows/macOS without editing the source.
2. **Latency that grows over time** — it reads the serial port in fixed `read(8192)` bites and processes *every* queued frame. If the host can't keep up (slow machine, Windows USB batching), frames pile up in the OS input buffer and you end up always rendering *old* data, so on-screen lag climbs and never recovers.
3. **Auto-gain display** — it normalizes every frame by that frame's own max (`contact / contact.max()`), so the colors are relative to whatever is happening right now. Absolute pressure is invisible and the image "breathes".
4. **One-shot baseline** — the baseline is captured once at startup with no re-zero and no drift handling.

`flexitac-live` addresses all four.

## Features

- **Portable serial**: auto-detects the port (FTDI / CH340 / CP210x / Arduino), or pass `--port`.
- **Bounded latency**: the reader always drains the serial buffer to the **newest complete frame** and drops the rest, so lag stays fixed no matter how slow the host is.
- **Absolute force scale**: value = `raw - baseline` in ADC counts (0 = no contact, 255 = full scale). Color saturation point is adjustable live with `+` / `-`.
- **Readable grid**: a border around every taxel, with row/column indices, and the numeric value printed in each cell.
- **Colorbar** mapping value to color.
- **Self-healing baseline**: untouched taxels slowly re-zero (kills drift/creep/temperature), while taxels in contact are frozen so their reading does not decay.
- **`sumF` readout**: the status line shows total force over the contact patch, which stays monotonic with grip force even when a single taxel saturates (see [Saturation](#a-note-on-saturation-fold-back)).
- **Raw diagnostic view** (`d`): shows the raw ADC values with no processing, useful for debugging wiring and sensor behavior.
- Works with the **16×32** sensor and the **32×32** dev board via `--rows` / `--cols`.

## Install

```bash
pip install numpy pyserial opencv-python
# or: pip install -r requirements.txt
```

## Firmware (not included here)

This repo is host-side only. Flash your board with the FlexiTac firmware from the upstream repo:
[FlexiTac/FlexiTac_Hardware_Repo](https://github.com/FlexiTac/FlexiTac_Hardware_Repo) (`32_16_fast.ino`).
The firmware is the authors' work and is licensed CC BY-NC 4.0 — get it from them, it is deliberately not redistributed here.

The wire format this tool expects: `0xAA 0x55` header + `rows*cols` bytes of 8-bit data, at 2,000,000 baud.

## Usage

```bash
python flexitac_live.py                       # auto-detect port
python flexitac_live.py --port COM3           # or /dev/ttyUSB0, /dev/tty.usbserial-*
python flexitac_live.py --rows 32 --cols 32   # 32x32 dev board
python flexitac_live.py --list                # list serial ports and exit
python flexitac_live.py --vmax 80 --scale 44 --alpha 0.6
```

**Keys in the window:**

| Key | Action |
|-----|--------|
| `q` / `Esc` | quit |
| `r` | re-zero the baseline |
| `n` | toggle per-taxel numbers |
| `d` | toggle raw / force view |
| `+` / `-` | change color saturation point (`vmax`) |
| `[` / `]` | change temporal smoothing (`alpha`) |

## How the latency fix works

Latency comes in two flavors:

- **Fixed latency** — a constant small offset from press to pixel. Harmless.
- **Accumulating latency** — the delay keeps growing until the view is seconds behind. This is the one people notice.

Accumulating latency happens when the host consumes frames slower than the board produces them (~100 Hz here). The unread bytes queue up in the OS serial buffer, which is FIFO, so every `read()` hands you the *oldest* frame. The longer you run, the further behind you get.

The fix is to treat the serial buffer as "latest value wins": each tick, read everything available (`in_waiting`), find the **last** complete frame in it, and discard the earlier ones. Latency then becomes `O(1)` regardless of host speed. A separate reader thread also keeps rendering from backpressuring the serial read.

On Windows there is one more fixed-latency source: USB-serial bridges (FTDI FT232, CH340) buffer bytes and only flush every *latency timer* interval, which defaults to **16 ms**. Set it to **1 ms** — either in Device Manager (COM port → Properties → Port Settings → Advanced → Latency Timer) or with the included `set_ftdi_latency.cmd` (FTDI). The remaining ~40–80 ms is the Velostat material's own response/recovery time and cannot be removed in software.

## A note on saturation (fold-back)

Press hard enough and a single taxel's value climbs, saturates, and can then **drop while you press harder**. That is a property of the resistive material and the readout, before the ADC — it is not a software bug and software cannot recover the lost information:

- Velostat's sensitivity collapses above ~3 N (roughly 250× less sensitive), so the curve flattens and noise/crosstalk take over.
- As force grows, the contact patch spreads and the load is shared with neighboring taxels, so the *pressure* at the center taxel can fall even as total force rises.

For this reason the status line reports **`sumF`** (total force over the contact patch), which stays monotonic with grip force even when the single-taxel peak folds back. If you need a single taxel to stay monotonic too, that is a hardware change: add a silicone/EVA force-spreading layer to keep the working point in the sensitive range.

## Credits

- **FlexiTac** — the sensor, hardware, and firmware are by **Binghao Huang** and **Yunzhu Li** (Columbia University). Project: <https://flexitac.github.io/> · Hardware/firmware: <https://github.com/FlexiTac/FlexiTac_Hardware_Repo> · Official Python package: <https://github.com/WT-MM/PyFlexiTac>
- This visualizer is an independent host-side tool and is not endorsed by the FlexiTac authors.

## License

[MIT](LICENSE) for the code in this repository. The FlexiTac firmware and hardware are licensed separately (CC BY-NC 4.0) by their authors and are not included here.
