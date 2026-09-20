#!/usr/bin/env python3
"""
flexitac-live -- a live viewer for FlexiTac tactile sensors that behaves on a laptop.

I got a FlexiTac sensor and the stock viewer (fast_32_16.py) assumes Linux and a
fast host: the port is hardcoded to /dev/ttyUSB0, and on Windows the heatmap kept
drifting further and further behind my finger the longer it ran. This is what I
ended up with after chasing that lag down and wanting to actually read numbers off
the pad instead of a auto-scaled blob.

It's a from-scratch host-side reader. It only speaks the sensor's documented serial
format (0xAA 0x55 header + rows*cols bytes of 8-bit data @ 2 Mbaud) -- no firmware
is reused or redistributed here; grab that from the upstream FlexiTac repo.

What it does differently from the stock script:
  - Auto-detects the serial port, so it runs on Windows/macOS too (not just Linux).
  - Latency stays bounded. The reader always jumps to the newest complete frame in
    the buffer instead of dutifully processing every queued frame, so lag can't
    snowball on a slow host (this is the fix that actually mattered).
  - Shows absolute force per taxel (raw minus baseline, in ADC counts) instead of
    normalizing every frame by its own max -- so the values mean something.
  - Draws a real grid with row/column indices, a number in every cell, and a colorbar.
  - The baseline self-heals where you're not touching, and freezes where you are, so
    it doesn't drift and it doesn't quietly eat a sustained press.

Usage:
    python flexitac_live.py                       # auto-detect port
    python flexitac_live.py --port COM3           # or /dev/ttyUSB0, /dev/tty.usbserial-*
    python flexitac_live.py --rows 32 --cols 32   # 32x32 dev board
    python flexitac_live.py --list                # just list serial ports and exit
    python flexitac_live.py --vmax 80 --scale 44 --alpha 0.6

Keys:  q/Esc quit  |  r re-zero baseline  |  n toggle numbers  |
       d raw/force view  |  +/- color range  |  [ / ] smoothing
"""
import argparse
import sys
import time
import threading

import numpy as np
import serial
import serial.tools.list_ports
import cv2

# The wire format is defined by the sensor firmware (upstream 32_16_fast.ino).
# Each frame is a two-byte sync marker followed by one byte per taxel, row-major.
BAUD = 2_000_000
ROWS, COLS = 16, 32                 # overridden by --rows/--cols for the 32x32 board
FRAME_BYTES = ROWS * COLS
MAGIC = b"\xAA\x55"                 # start-of-frame marker, lets us re-sync after a glitch
INIT_FRAMES = 20                    # frames averaged into the startup baseline

# Two knobs worth understanding if you tune this:
#   CONTACT_ON  - how many counts above baseline we treat as a real touch. Below it we
#                 assume the taxel is idle and let its baseline drift; above it we freeze
#                 the baseline so a hard press doesn't slowly get subtracted away.
#   BASE_BETA   - how fast idle taxels re-zero themselves. Small = stable but slow to
#                 forget drift; large = tracks drift fast but can nibble at light touches.
CONTACT_ON = 6.0
BASE_BETA = 0.02
FULL_SCALE = 255.0                  # 8-bit ADC, so a taxel can never read above this

# The reader runs in its own thread; these hand the latest frame to the display loop.
_state_lock = threading.Lock()
_latest_force = np.zeros((ROWS, COLS), dtype=np.float32)
_latest_raw = np.zeros((ROWS, COLS), dtype=np.float32)
_baseline_ready = False
_recalibrate = threading.Event()
_stop = threading.Event()
_reader_fps = 0.0


def process_frame(raw, baseline):
    """Turn a raw frame into force counts, and nudge the baseline.

    The trick that makes a sustained press hold steady instead of fading: only the
    taxels that are NOT being touched are allowed to drift toward the current reading.
    Anything actually in contact keeps its old baseline frozen, so `raw - baseline`
    stays honest. (A press will still creep down a little under constant load -- that's
    the Velostat material relaxing, not this code.)
    """
    signal = raw - baseline
    free = signal < CONTACT_ON
    baseline[free] += BASE_BETA * (raw[free] - baseline[free])
    force = np.clip(signal, 0.0, FULL_SCALE)
    return force, baseline


def list_serial_ports():
    return list(serial.tools.list_ports.comports())


def pick_port(preferred=None):
    """Best-effort guess at which port is the sensor.

    FlexiTac reading boards show up behind a USB-serial bridge -- genuine Nanos use an
    FTDI FT232, clones use a CH340, some use CP210x -- so we match on those before
    falling back to whatever's plugged in.
    """
    ports = list_serial_ports()
    if preferred:
        return preferred
    if not ports:
        return None
    keywords = ("CH340", "CH341", "USB-SERIAL", "USB SERIAL", "USB Serial",
                "Arduino", "wch", "Silicon Labs", "CP210", "FTDI", "FT232")
    for p in ports:
        desc = f"{p.description} {p.manufacturer or ''} {p.hwid or ''}"
        if any(k.lower() in desc.lower() for k in keywords):
            return p.device
    return ports[0].device


def _drain_latest_frame(serDev, ring):
    """Pull everything waiting on the port and return only the NEWEST complete frame.

    This is the whole latency story. The board streams ~100 frames/sec; if the display
    can't keep up, unread bytes queue in the OS buffer, and since that queue is FIFO a
    naive read() hands you the oldest frame first -- so you fall further behind every
    second. By emptying the buffer each tick and keeping just the last frame we've seen,
    the delay stops accumulating no matter how slow the host is.
    """
    n = serDev.in_waiting
    chunk = serDev.read(n if n > 0 else 1)
    if chunk:
        ring.extend(chunk)
    if len(ring) > 200_000:            # paranoia cap so a stall can't grow this forever
        del ring[:-200_000]

    last = None
    while True:
        idx = ring.find(MAGIC)
        if idx < 0:
            if len(ring) > 1:
                del ring[:-1]          # keep one byte in case the marker was split
            break
        if idx > 0:
            del ring[:idx]             # drop junk before the marker
        if len(ring) < 2 + FRAME_BYTES:
            break                      # rest of the frame hasn't arrived yet
        del ring[:2]
        last = bytes(ring[:FRAME_BYTES])   # overwrite -> only the newest survives
        del ring[:FRAME_BYTES]
    return last


def reader_thread(serDev):
    # Kept on its own thread so a slow cv2 render can never back-pressure the serial
    # read. The display just grabs whatever the latest frame is whenever it's ready.
    global _latest_force, _latest_raw, _baseline_ready, _reader_fps
    ring = bytearray()
    serDev.timeout = 0.005

    def collect_baseline():
        # Median over a few frames = the "no contact" reference. Heads up: don't be
        # touching the pad while this runs, or your press becomes part of zero. Hit 'r'
        # to redo it if you started dirty.
        frames = []
        while len(frames) < INIT_FRAMES and not _stop.is_set():
            f = _drain_latest_frame(serDev, ring)
            if f is None:
                time.sleep(0.001)
                continue
            frames.append(np.frombuffer(f, dtype=np.uint8)
                          .reshape((ROWS, COLS)).astype(np.float32))
        if not frames:
            return np.zeros((ROWS, COLS), dtype=np.float32)
        return np.median(np.stack(frames, 0), axis=0)

    baseline = collect_baseline()
    with _state_lock:
        _baseline_ready = True
    print("[reader] baseline ready. streaming...")

    t_prev = time.time()
    while not _stop.is_set():
        if _recalibrate.is_set():      # 'r' pressed in the window
            _recalibrate.clear()
            with _state_lock:
                _baseline_ready = False
            baseline = collect_baseline()
            with _state_lock:
                _baseline_ready = True
            print("[reader] baseline re-zeroed.")

        f = _drain_latest_frame(serDev, ring)
        if f is None:
            time.sleep(0.001)
            continue

        raw = np.frombuffer(f, dtype=np.uint8).reshape((ROWS, COLS)).astype(np.float32)
        force, baseline = process_frame(raw, baseline)

        now = time.time()
        fps = 1.0 / max(now - t_prev, 1e-6)
        t_prev = now
        with _state_lock:
            _latest_force = force
            _latest_raw = raw
            _reader_fps = 0.9 * _reader_fps + 0.1 * fps   # smoothed, just for the readout


# ---- rendering ----
# Layout: a strip on the left for row numbers, a strip on top for column numbers, the
# heatmap in the middle, and a colorbar on the right. Everything is drawn onto one canvas.
MARGIN_L = 30
MARGIN_T = 24
CBAR_W = 74
STATUS_H = 26
GRID = (60, 60, 60)


def _colorbar(height, vmax):
    # A vertical gradient with a handful of tick labels so a color can be read back as
    # an approximate number. Rebuilt each frame because vmax can change live with +/-.
    grad = np.linspace(255, 0, height).astype(np.uint8).reshape(height, 1)
    strip = cv2.applyColorMap(np.repeat(grad, 22, axis=1), cv2.COLORMAP_VIRIDIS)
    canvas = np.zeros((height, CBAR_W, 3), dtype=np.uint8)
    canvas[:, :22] = strip
    for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
        y = int((1 - frac) * (height - 1))
        y = min(max(y, 8), height - 4)
        cv2.putText(canvas, str(int(round(frac * vmax))), (26, y + 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, (230, 230, 230), 1, cv2.LINE_AA)
    return canvas


def render(disp, force, fps, vmax, show_numbers, scale, mode_label):
    # `disp` is what we color/number (force, or raw in diagnostic mode); `force` is
    # always the processed signal so the status line stays consistent between views.
    heat_w, heat_h = COLS * scale, ROWS * scale
    total_w = MARGIN_L + heat_w + CBAR_W
    total_h = MARGIN_T + heat_h + STATUS_H
    canvas = np.zeros((total_h, total_w, 3), dtype=np.uint8)

    cnorm = np.clip(disp / max(vmax, 1e-6), 0, 1)
    img8 = (cnorm * 255).astype(np.uint8)
    # NEAREST, not a smooth resize -- I want to see the actual taxels, not a blurred blob.
    big = cv2.resize(img8, (heat_w, heat_h), interpolation=cv2.INTER_NEAREST)
    heat = cv2.applyColorMap(big, cv2.COLORMAP_VIRIDIS)
    canvas[MARGIN_T:MARGIN_T + heat_h, MARGIN_L:MARGIN_L + heat_w] = heat

    for c in range(COLS + 1):          # vertical grid lines
        x = MARGIN_L + c * scale
        cv2.line(canvas, (x, MARGIN_T), (x, MARGIN_T + heat_h), GRID, 1)
    for r in range(ROWS + 1):          # horizontal grid lines
        y = MARGIN_T + r * scale
        cv2.line(canvas, (MARGIN_L, y), (MARGIN_L + heat_w, y), GRID, 1)

    for c in range(COLS):              # column numbers across the top
        cv2.putText(canvas, str(c), (MARGIN_L + c * scale + 2, 16),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.34, (180, 180, 180), 1, cv2.LINE_AA)
    for r in range(ROWS):              # row numbers down the left
        cv2.putText(canvas, str(r), (2, MARGIN_T + r * scale + int(scale * 0.6)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.34, (180, 180, 180), 1, cv2.LINE_AA)

    # The per-cell numbers only fit once cells are big enough; skip them on tiny scales.
    if show_numbers and scale >= 22:
        for r in range(ROWS):
            for c in range(COLS):
                v = int(disp[r, c])
                bright = cnorm[r, c] > 0.55
                col = (0, 0, 0) if bright else (210, 210, 210)   # dark text on hot cells
                if v == 0 and not bright:
                    col = (110, 110, 110)                        # mute the idle zeros
                tx = MARGIN_L + c * scale + int(scale * 0.14)
                ty = MARGIN_T + r * scale + int(scale * 0.62)
                cv2.putText(canvas, str(v), (tx, ty),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.32, col, 1, cv2.LINE_AA)

    cb = _colorbar(heat_h, vmax)
    canvas[MARGIN_T:MARGIN_T + heat_h, MARGIN_L + heat_w:MARGIN_L + heat_w + CBAR_W] = cb

    # A single taxel folds back once the material saturates, so `peak` can drop under a
    # harder press. `sumF` (total over the contact patch) keeps rising with real force,
    # which is why it's here -- it's the number to trust for "how hard".
    peak = int(disp.max())
    contacts = int((force > CONTACT_ON).sum())
    total = int(force.sum())
    status = (f"{mode_label} FPS:{fps:4.0f} vmax:{int(vmax)} peak:{peak} "
              f"sumF:{total} contacts:{contacts}  (d=raw n=num r=zero +/-vmax)")
    cv2.putText(canvas, status, (MARGIN_L, total_h - 8),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1, cv2.LINE_AA)
    return canvas


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--rows", type=int, default=16, help="sensor rows (default 16)")
    ap.add_argument("--cols", type=int, default=32, help="sensor cols (default 32)")
    ap.add_argument("--alpha", type=float, default=0.6,
                    help="temporal smoothing 0..1 (higher=less lag). default 0.6")
    ap.add_argument("--scale", type=int, default=40, help="pixels per taxel")
    ap.add_argument("--vmax", type=float, default=100.0,
                    help="color saturates at this many counts (default 100)")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    # rows/cols feed a lot of module-level sizing, so set them once, up front.
    global ROWS, COLS, FRAME_BYTES
    ROWS, COLS = args.rows, args.cols
    FRAME_BYTES = ROWS * COLS

    ports = list_serial_ports()
    if args.list:
        if not ports:
            print("No serial ports found.")
        for p in ports:
            print(f"  {p.device:8s}  {p.description}  [{p.hwid}]")
        return

    port = pick_port(args.port)
    if port is None:
        print("!! No serial port found. Plug in the FlexiTac reading board,")
        print("   then re-run.  (python flexitac_live.py --list)")
        sys.exit(1)

    print(f"[main] detected ports: {[p.device for p in ports]}")
    print(f"[main] using port: {port} @ {BAUD} baud")
    try:
        serDev = serial.Serial(port, BAUD, timeout=0.005)
    except serial.SerialException as e:
        # Almost always "port already open" -- an Arduino Serial Monitor left running.
        print(f"!! Could not open {port}: {e}")
        print("   Close any Arduino Serial Monitor / other program using the port.")
        sys.exit(1)

    serDev.reset_input_buffer()
    th = threading.Thread(target=reader_thread, args=(serDev,), daemon=True)
    th.start()

    win = "flexitac-live"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, MARGIN_L + COLS * args.scale + CBAR_W,
                     MARGIN_T + ROWS * args.scale + STATUS_H)

    prev = np.zeros((ROWS, COLS), dtype=np.float32)
    vmax = args.vmax
    alpha = args.alpha
    show_numbers = True
    show_raw = False
    print("[main] window open. press the sensor AFTER 'streaming' appears.")

    while True:
        with _state_lock:
            ready = _baseline_ready
            force = _latest_force.copy()
            raw = _latest_raw.copy()
            fps = _reader_fps

        # Light exponential smoothing on the force view -- takes the jitter off without
        # adding real lag. The raw diagnostic view is shown unsmoothed on purpose.
        prev = alpha * force + (1 - alpha) * prev
        if show_raw:
            img = render(raw, prev, fps if ready else 0.0, 255.0, show_numbers, args.scale, "RAW  ")
        else:
            img = render(prev, prev, fps if ready else 0.0, vmax, show_numbers, args.scale, "FORCE")
        if not ready:
            cv2.putText(img, "initializing baseline...", (MARGIN_L + 6, MARGIN_T + 20),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.imshow(win, img)

        k = cv2.waitKey(1) & 0xFF
        if k in (ord('q'), 27):
            break
        elif k == ord('r'):
            _recalibrate.set()
        elif k == ord('n'):
            show_numbers = not show_numbers
        elif k == ord('d'):
            show_raw = not show_raw
        elif k in (ord('+'), ord('=')):
            vmax = min(vmax + 10, 255)
        elif k in (ord('-'), ord('_')):
            vmax = max(vmax - 10, 10)
        elif k == ord(']'):
            alpha = min(alpha + 0.1, 1.0)
        elif k == ord('['):
            alpha = max(alpha - 0.1, 0.1)

    _stop.set()
    time.sleep(0.05)
    serDev.close()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
