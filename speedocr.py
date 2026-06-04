"""Lightweight speed-readout OCR for FH6 (no Pillow / no ML).

Pipeline: capture the speed region (screen.grab_luma) -> binarize on brightness
-> segment into digit columns -> normalize each glyph to a fixed grid -> classify
against templates built once at the user's resolution (build_speed_templates.py).

Templates live in speed_templates.json next to the config (per-resolution). The
font is center-aligned and stylized, so we segment by column gaps and match by
normalized-bitmap overlap. Everything is integer/list math.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import screen

GW, GH = 14, 20  # normalized glyph grid (cols x rows)
_BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
TEMPLATES_PATH = _BASE / "speed_templates.json"


def binarize(luma: list[int], thresh: int) -> list[int]:
    return [1 if v >= thresh else 0 for v in luma]


def _col_bright(bw: list[int], w: int, h: int) -> list[int]:
    """Bright-pixel count per column."""
    return [sum(bw[y * w + x] for y in range(h)) for x in range(w)]


def segment(bw: list[int], w: int, h: int, min_col: int = 1, gap_merge: int = 2) -> list[tuple[int, int]]:
    """Return [(x0, x1), ...] column ranges of digit glyphs, left to right.
    Columns with >= min_col bright pixels are 'ink'; runs separated by small gaps
    are merged (handles thin internal gaps of a glyph)."""
    cols = _col_bright(bw, w, h)
    runs: list[list[int]] = []
    x = 0
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
    # drop specks (too thin to be a digit)
    return [(a, b) for a, b in merged if (b - a + 1) >= 2]


def normalize(bw: list[int], w: int, h: int, x0: int, x1: int) -> list[int]:
    """Crop the glyph (x0..x1 + its vertical ink extent) and resample to GW x GH."""
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


def _ink_height(mask: list[int], w: int, h: int, a: int, b: int) -> int:
    ys = [y for y in range(h) for x in range(a, b + 1) if mask[y * w + x]]
    return (max(ys) - min(ys) + 1) if ys else 0


def glyphs_from_region(rect: tuple[int, int, int, int], min_v: int = 200, sat_tol: int = 45,
                       min_h_frac: float = 0.45, max_w_frac: float = 0.36):
    """Capture and return (segments, normalized_glyphs, w, h, mask). Uses a WHITE
    mask (near-white, low saturation) so the white speed text survives bright,
    coloured backgrounds. Segments are then filtered to keep only big digit-shaped
    blobs (tall enough, not too wide) — this rejects the gauge's small dial numbers
    and stray cloud/reflection specks that also read as white."""
    w, h, mask = screen.grab_white(rect, min_v, sat_tol)
    if w == 0:
        return [], [], 0, 0, []
    min_h = min_h_frac * h
    max_w = max_w_frac * w
    segs = [(a, b) for a, b in segment(mask, w, h)
            if (b - a + 1) <= max_w and _ink_height(mask, w, h, a, b) >= min_h]
    glyphs = [normalize(mask, w, h, a, b) for a, b in segs]
    return segs, glyphs, w, h, mask


def ascii_glyph(glyph: list[int]) -> str:
    return "\n".join("".join("#" if glyph[y * GW + x] else "." for x in range(GW)) for y in range(GH))


def _score(a: list[int], b: list[int]) -> float:
    return sum(1 for x, y in zip(a, b) if x == y) / float(len(a))


def classify(glyph: list[int], templates: dict[str, list[int]], min_score: float = 0.80):
    """Return (digit:int, score) of best matching template, or (None, score)."""
    best_d, best_s = None, 0.0
    for d, tpl in templates.items():
        s = _score(glyph, tpl)
        if s > best_s:
            best_d, best_s = d, s
    if best_d is None or best_s < min_score:
        return None, best_s
    return int(best_d), best_s


def load_templates() -> dict[str, list[int]]:
    try:
        with TEMPLATES_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_templates(templates: dict[str, list[int]]) -> None:
    with TEMPLATES_PATH.open("w", encoding="utf-8") as f:
        json.dump(templates, f)


def read_speed(rect: tuple[int, int, int, int], templates: dict[str, list[int]],
               min_v: int = 200, sat_tol: int = 45, min_score: float = 0.80) -> int | None:
    """Read the speed integer from the region, or None if unreadable."""
    if not templates:
        return None
    _, glyphs, _, _, _ = glyphs_from_region(rect, min_v, sat_tol)
    if not glyphs or len(glyphs) > 3:
        return None
    digits = []
    for g in glyphs:
        d, _ = classify(g, templates, min_score)
        if d is None:
            return None
        digits.append(str(d))
    try:
        return int("".join(digits))
    except ValueError:
        return None
