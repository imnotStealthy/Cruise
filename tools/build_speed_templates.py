"""Build/extend the speed-OCR digit templates from on-screen speed (two steps).

The speed changes too fast to label live, so capture freezes the glyphs first:

  1) While a race shows the speed, capture (freezes glyphs + prints ASCII):
       python tools/build_speed_templates.py capture [thresh] [x0 y0 x1 y1]
  2) Read the digits off the ASCII (or your screenshot) and label them:
       python tools/build_speed_templates.py label 231

Repeat capture+label at different speeds until digits 0-9 are all covered.
Templates merge into speed_templates.json (per-resolution). On a segment-count
mismatch, tune REGION/thresh so each digit is exactly one segment.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot       # noqa: E402
import speedocr  # noqa: E402

REGION = [0.875, 0.905, 0.985, 0.985]
THRESH = 200  # min brightness for the WHITE mask (near-white isolation)
TMP = Path(__file__).with_name("speed_glyphs_tmp.json")


def _capture(thresh: int) -> None:
    cfg = bot.load_config()
    bot.select_game_window(cfg)
    ox, oy, w, h = bot.detection_rect(cfg)
    rect = (ox + int(REGION[0] * w), oy + int(REGION[1] * h),
            int((REGION[2] - REGION[0]) * w), int((REGION[3] - REGION[1]) * h))
    segs, glyphs, gw, gh, _ = speedocr.glyphs_from_region(rect, thresh)
    print(f"region_px={rect} captured={gw}x{gh} segments={len(segs)} thresh={thresh}")
    for i, g in enumerate(glyphs):
        print(f"--- glyph {i} (cols {segs[i][0]}-{segs[i][1]}) ---")
        print(speedocr.ascii_glyph(g))
    TMP.write_text(json.dumps({"glyphs": glyphs, "thresh": thresh}), encoding="utf-8")
    print(f"froze {len(glyphs)} glyph(s). Now: build_speed_templates.py label <speed>")


def _label(speed: str) -> None:
    if not TMP.exists():
        print("no frozen capture. Run 'capture' first.")
        return
    glyphs = json.loads(TMP.read_text(encoding="utf-8"))["glyphs"]
    if len(glyphs) != len(speed):
        print(f"MISMATCH: {len(glyphs)} glyph(s) vs {len(speed)} digit(s). Re-capture/tune region.")
        return
    templates = speedocr.load_templates()
    for ch, g in zip(speed, glyphs):
        templates[ch] = g
    speedocr.save_templates(templates)
    have = "".join(sorted(templates.keys()))
    missing = "".join(d for d in "0123456789" if d not in templates)
    print(f"labelled '{speed}'. covered: {have} ({len(templates)}/10). missing: {missing or 'none'}")


def main() -> None:
    a = sys.argv[1:]
    if a and a[0] == "capture":
        thresh = int(a[1]) if len(a) >= 2 else THRESH
        if len(a) >= 6:
            REGION[:] = [float(x) for x in a[2:6]]
        _capture(thresh)
    elif len(a) >= 2 and a[0] == "label" and a[1].isdigit():
        _label(a[1])
    else:
        print("usage: build_speed_templates.py capture [thresh] [x0 y0 x1 y1] | label <speed>")


if __name__ == "__main__":
    main()
