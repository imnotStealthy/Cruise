"""Locate the FH6 speed readout for the OCR speed-cap (read-only).

Run it WHILE a race is on screen with the km/h number visible (bottom-right).
It captures the configured region (fractions of the FH6 window) and prints an
ASCII preview so we can confirm the digits are framed and pick a brightness
threshold. Tune the REGION below until the number fills the box cleanly.

Usage: python tools/calibrate_speed.py [x0] [y0] [x1] [y1] [thresh]
Defaults frame the bottom-right speed number. Saves tools/speed_region.txt.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot   # noqa: E402
import screen  # noqa: E402

# Fractional region of the FH6 window framing the big km/h number (bottom-right).
REGION = [0.905, 0.895, 0.996, 0.985]
THRESH = 150
COLS = 90  # ASCII preview width


def main() -> None:
    args = sys.argv[1:]
    if len(args) >= 4:
        REGION[:] = [float(a) for a in args[:4]]
    thresh = int(args[4]) if len(args) >= 5 else THRESH

    cfg = bot.load_config()
    bot.select_game_window(cfg)
    ox, oy, w, h = bot.detection_rect(cfg)
    rx = (ox + int(REGION[0] * w), oy + int(REGION[1] * h))
    rw = int((REGION[2] - REGION[0]) * w)
    rh = int((REGION[3] - REGION[1]) * h)

    print(f"window={w}x{h} region_px=({rx[0]},{rx[1]}) size={rw}x{rh} thresh={thresh}")
    time.sleep(1.0)
    gw, gh, luma = screen.grab_luma((rx[0], rx[1], rw, rh))
    if gw == 0:
        print("empty capture")
        return

    # ASCII preview: downsample to COLS wide, keep aspect.
    step = max(1, gw // COLS)
    rows = []
    for yy in range(0, gh, step * 2):  # *2 vertical: chars are tall
        line = []
        for xx in range(0, gw, step):
            line.append("#" if luma[yy * gw + xx] >= thresh else " ")
        rows.append("".join(line))
    preview = "\n".join(rows)
    bright = sum(1 for v in luma if v >= thresh)
    print(preview)
    print(f"bright_px={bright}/{gw * gh} ({100 * bright // max(1, gw * gh)}%)")
    Path(__file__).with_name("speed_region.txt").write_text(
        f"REGION={REGION} thresh={thresh}\n\n{preview}\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
