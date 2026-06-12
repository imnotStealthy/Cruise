"""Calibrate the Buy Car price OCR (learn the FH6 menu digit font), READ-ONLY.

While FH6 shows the Buy Car popup ("Do you want to buy this Car for CR X?"):

  py -3 tools/calibrate_price.py            # capture + show what it segments
  py -3 tools/calibrate_price.py 346750     # map the glyphs to this known price

Run it on a few cars with different prices until digits 0-9 are all learned;
templates merge into ~/.cruise/buyer_price_templates.json. The buyer then reads
the price on screen and subtracts it per purchase (no memory scan needed).
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import screen  # noqa: E402
import window  # noqa: E402
import bot as core  # noqa: E402

GW, GH = 14, 20
OUT = Path.home() / ".cruise" / "buyer_price_templates.json"
# Price line region (fractions of the FH6 window). The price sits on the right
# half of the centred sentence; this band spans it. Refine if segmentation is off.
RECT = (0.42, 0.478, 0.24, 0.025)


def _segment(bw, w, h, min_col=1, gap_merge=2):
    cols = [sum(bw[y * w + x] for y in range(h)) for x in range(w)]
    runs, x = [], 0
    while x < w:
        if cols[x] >= min_col:
            x0 = x
            while x < w and cols[x] >= min_col:
                x += 1
            runs.append([x0, x - 1])
        else:
            x += 1
    if not runs:
        return []
    merged = [runs[0]]
    for r in runs[1:]:
        if r[0] - merged[-1][1] <= gap_merge:
            merged[-1][1] = r[1]
        else:
            merged.append(r)
    return [(a, b) for a, b in merged if (b - a + 1) >= 2]


def _normalize(bw, w, h, x0, x1):
    ys = [y for y in range(h) for x in range(x0, x1 + 1) if bw[y * w + x]]
    if not ys:
        return [0] * (GW * GH)
    y0, y1 = min(ys), max(ys)
    cw, ch = x1 - x0 + 1, y1 - y0 + 1
    out = [0] * (GW * GH)
    for gy in range(GH):
        sy = y0 + gy * ch // GH
        for gx in range(GW):
            sx = x0 + gx * cw // GW
            out[gy * GW + gx] = bw[sy * w + sx]
    return out


def main() -> None:
    cfg = core.load_config()
    win = window.select_game_window(cfg)
    rect = win[3] if win else (0, 0, *screen.size())
    ox, oy, w, h = rect
    region = (ox + int(RECT[0] * w), oy + int(RECT[1] * h), int(RECT[2] * w), int(RECT[3] * h))
    screen.save_png(str(Path.home() / ".cruise" / "price_region.png"), region)
    rw, rh, mask = screen.grab_white(region, min_v=170, sat_tol=60)
    if rw == 0:
        print("capture failed (FH6 not found?)")
        return
    min_h = 0.5 * rh
    segs = []
    for a, b in _segment(mask, rw, rh):
        ys = [y for y in range(rh) for x in range(a, b + 1) if mask[y * rw + x]]
        if ys and (max(ys) - min(ys) + 1) >= min_h:
            segs.append((a, b))
    # The rightmost tall glyph is the trailing "?" — drop it; commas were
    # already dropped (too short). What remains, left to right, are the digits.
    if segs:
        segs = segs[:-1]
    print(f"region px {rw}x{rh}; {len(segs)} digit glyph(s) after dropping '?' "
          f"(saved ~/.cruise/price_region.png)")
    for a, b in segs:
        g = _normalize(mask, rw, rh, a, b)
        print(f"  glyph x{a}-{b}:")
        print("\n".join("    " + "".join("#" if g[y * GW + x] else "." for x in range(GW)) for y in range(GH)))

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("\nRe-run with the price (digits only), e.g.: calibrate_price.py 346750")
        return
    digits = [c for c in args[0] if c.isdigit()]
    if len(segs) != len(digits):
        print(f"\nMISMATCH: {len(segs)} digit glyphs vs {len(digits)} digits in '{args[0]}'. "
              "Adjust RECT (or the popup wasn't showing). See price_region.png.")
        return
    tpl = {}
    try:
        tpl = json.loads(OUT.read_text())
    except Exception:
        pass
    for (a, b), d in zip(segs, digits):
        tpl[d] = _normalize(mask, rw, rh, a, b)
    OUT.write_text(json.dumps(tpl))
    print(f"\nsaved {len(tpl)} digit template(s) -> {OUT}  (have: {''.join(sorted(tpl))})")


if __name__ == "__main__":
    main()
