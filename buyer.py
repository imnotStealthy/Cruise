"""Garage Buyer: automated Car Collection / Autoshow purchases.

Separate runner from the Skill Points loop (bot.run). Flow per car:
Car Collection -> Space -> Autoshow confirm (Yes) -> Buy Car (read price, Enter)
-> "added to your garage" (Enter) -> next car (Right) -> repeat.

Safety: focus guard (nothing sent when FH6 is not foreground), mouse-corner
failsafe, never buys when the price cannot be read or the credit estimate
would drop below the reserve. Price OCR reuses the speed-OCR pipeline
(white-mask + per-resolution digit templates from tools/build_speed_templates).
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import inputs
import memory
import screen
import window

# ---- visual anchors (fractions of the FH6 window) -----------------------
# Lime popup headers (202,255,2). The three popups have their header bar at
# distinct heights, so one narrow band per popup is enough to tell them apart.
_LIME = (202, 255, 2)
_HDR = {  # name -> band y
    "autoshow_confirm": 0.372,   # "Car Collection" popup (Yes/No)
    "buy_car": 0.425,            # "Buy Car" popup (Buy / Vouchers)
    "added_to_garage": 0.467,    # "Car has been added to your garage."
}
_HDR_X0, _HDR_X1, _HDR_SAMPLES, _HDR_MIN_HITS, _HDR_TOL = 0.38, 0.60, 40, 20, 40
# Option rows (x=0.5): selected row is black-filled, unselected is white.
_ROWS = {
    "autoshow_confirm": (0.576, 0.626),  # (No, Yes)
    "buy_car": (0.540, 0.591),           # (Buy, Buy Vouchers)
}
# Car Collection base screen: the two orange nav arrows. Each is a circle with
# a dark triangle in the middle, so a single centre pixel can land on the dark
# glyph -> we scan a short horizontal segment across each arrow and count orange
# pixels (robust to the triangle and small layout shifts).
_ARROW_SEGS = ((0.155, 0.200, 0.345), (0.800, 0.845, 0.345))  # (x0, x1, y)
_ARROW_RGB = (243, 103, 27)
_ARROW_TOL = (45, 55, 45)
_ARROW_SAMPLES = 46
_ARROW_MIN_HITS = 6
# Price text region inside the Buy Car popup ("... for CR 346,750?").
# Must match tools/calibrate_price.py RECT so the learned templates line up.
_PRICE_RECT = (0.42, 0.478, 0.24, 0.025)  # x, y, w, h fractions

# ---- digit OCR (same pipeline as tools/speedocr.py, inlined: tools/ is not
# bundled in the frozen exe) ----------------------------------------------
GW, GH = 14, 20
_BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent


def _load_templates() -> dict:
    for p in (_BASE / "speed_templates.json", _BASE / "tools" / "speed_templates.json",
              Path.home() / ".cruise" / "speed_templates.json"):
        try:
            with p.open(encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            continue
    return {}


def _load_price_templates() -> dict:
    """Digit templates for the Buy Car price font, built by
    tools/calibrate_price.py. Falls back to the speed templates if absent."""
    try:
        with (Path.home() / ".cruise" / "buyer_price_templates.json").open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return _load_templates()


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


def _classify(glyph, templates, min_score=0.82):
    best_d, best_s = None, 0.0
    for d, tpl in templates.items():
        s = sum(1 for a, b in zip(glyph, tpl) if a == b) / float(len(glyph))
        if s > best_s:
            best_d, best_s = d, s
    return (int(best_d), best_s) if best_d is not None and best_s >= min_score else (None, best_s)


def read_price(rect: tuple[int, int, int, int], templates: dict) -> int | None:
    """Read the displayed price from the Buy Car popup, or None.

    The band holds a full white sentence; digits are taken as the longest
    trailing run of template-recognized glyphs (commas/'?' segment as short or
    unrecognized glyphs and are skipped). If a discount is shown, the final
    (discounted) price is what FH6 displays here, so that is what we read.
    """
    if not templates:
        return None
    ox, oy, w, h = rect
    region = (ox + int(_PRICE_RECT[0] * w), oy + int(_PRICE_RECT[1] * h),
              int(_PRICE_RECT[2] * w), int(_PRICE_RECT[3] * h))
    rw, rh, mask = screen.grab_white(region, min_v=170, sat_tol=60)
    if rw == 0:
        return None
    min_h = 0.5 * rh
    segs = []
    for a, b in _segment(mask, rw, rh):
        ys = [y for y in range(rh) for x in range(a, b + 1) if mask[y * rw + x]]
        if ys and (max(ys) - min(ys) + 1) >= min_h:
            segs.append((a, b))
    digits = []
    for a, b in reversed(segs):  # right to left: price is the trailing run
        d, _s = _classify(_normalize(mask, rw, rh, a, b), templates)
        if d is None:
            if digits:
                break  # left edge of the number reached
            continue  # still skipping the trailing '?'
        digits.append(str(d))
    if len(digits) < 4:  # cheapest cars are 4+ digits; fewer = misread
        return None
    return int("".join(reversed(digits)))


def read_cr_yellow(rect: tuple[int, int, int, int], region_frac, templates: dict) -> int | None:
    """One-shot read of the yellow CR balance in region_frac (x,y,w,h fractions
    of the FH6 window). Same digit pipeline, yellow mask. None if no readable
    number (e.g. the HUD is hidden by a menu)."""
    if not templates:
        return None
    ox, oy, w, h = rect
    region = (ox + int(region_frac[0] * w), oy + int(region_frac[1] * h),
              int(region_frac[2] * w), int(region_frac[3] * h))
    rw, rh, mask = screen.grab_yellow(region)
    if rw == 0:
        return None
    min_h = 0.5 * rh
    segs = []
    for a, b in _segment(mask, rw, rh):
        ys = [y for y in range(rh) for x in range(a, b + 1) if mask[y * rw + x]]
        if ys and (max(ys) - min(ys) + 1) >= min_h:
            segs.append((a, b))
    digits = []
    for a, b in segs:  # left to right: CR balance is a single grouped number
        d, _s = _classify(_normalize(mask, rw, rh, a, b), templates)
        if d is not None:
            digits.append(str(d))
    if len(digits) < 4:
        return None
    return int("".join(digits))


class CreditSource:
    """Live credit balance for the buyer. mode 'memory' reads the value from
    process memory (located once per session by scanning for the seed value the
    user gave — addresses are stable within a session, no static chain needed);
    mode 'ocr' reads the yellow CR HUD when visible. 'manual' just tracks the
    estimate locally. Strictly read-only in all modes."""

    def __init__(self, cfg: dict, mode: str, seed: int, log, background: bool = True) -> None:
        self.mode = mode
        self.log = log
        self.cfg = cfg
        self.templates = _load_templates()
        self.cr_rect = cfg.get("buyer_cr_ocr_rect", [0.0, 0.0, 0.30, 0.10])
        self._handle = None
        self._cands: list[int] = []
        self._best: int | None = None  # best candidate address (memory mode)
        self._baseline = seed          # last known balance (to spot the drop)
        self._locating = False         # background scan in progress
        self.pid = None
        if mode == "memory":
            self._init_memory(seed, background)

    def _init_memory(self, seed: int, background: bool) -> None:
        name = self.cfg.get("buyer_mem_module", self.cfg.get("game_process", "ForzaHorizon6.exe"))
        pid = memory.pid_of(name)
        if pid is None:
            self.log("[buyer] memory: process not found; credit tracking off.")
            self.mode = "manual"
            return
        self._handle = memory.open_readonly(pid)
        if not self._handle:
            self.log("[buyer] memory: OpenProcess failed; credit tracking off.")
            self.mode = "manual"
            return
        self.pid = pid
        # Reuse addresses located earlier this FH6 session (cached) -> instant.
        prelocated = self.cfg.get("buyer_mem_located") or []
        if prelocated:
            cands = [a for a in prelocated if memory.read_u32(self._handle, a) == seed]
            if cands:
                self._cands = cands
                if len(cands) == 1:
                    self._best = cands[0]
                self.log(f"[buyer] memory: reused {len(cands)} cached address(es) for {seed}.")
                return

        def _scan():
            cands = memory.scan_u32(self._handle, seed, max_hits=64)
            self._cands = cands
            if len(cands) == 1:
                self._best = cands[0]
            self._locating = False
            self.log(f"[buyer] memory: located {len(cands)} candidate(s) for {seed}."
                     if cands else f"[buyer] memory: value {seed} not found; tracking off.")

        self._locating = True
        if background:
            threading.Thread(target=_scan, daemon=True).start()
        else:
            _scan()  # synchronous (used by the LOCATE CR button; ~minutes)

    def read(self) -> int | None:
        if self.mode == "memory" and self._handle:
            if self._best is not None:
                v = memory.read_u32(self._handle, self._best)
                if v is not None:
                    self._baseline = v
                return v
            # Not yet locked: before the first buy all candidates still hold the
            # same value -> consensus is the real balance.
            seen = [v for v in (memory.read_u32(self._handle, a) for a in self._cands)
                    if v is not None]
            if seen and all(v == seen[0] for v in seen):
                self._baseline = seen[0]
                return seen[0]
            return None
        if self.mode == "ocr":
            win = window.select_game_window(self.cfg)
            rect = win[3] if win else (0, 0, *screen.size())
            return read_cr_yellow(rect, self.cr_rect, self.templates)
        return None

    def on_purchase(self) -> None:
        """Call once the post-buy balance has updated. In memory mode, the real
        credit address is the candidate whose value DROPPED below the baseline;
        decoys keep the old value. Locks it when exactly one dropped."""
        if self.mode != "memory" or not self._handle or self._best is not None:
            return
        dropped = [a for a in self._cands
                   if (v := memory.read_u32(self._handle, a)) is not None and v < self._baseline]
        if len(dropped) == 1:
            self._best = dropped[0]
            self.log("[buyer] memory: credit address locked.")
        elif dropped:
            self._cands = dropped

    def close(self) -> None:
        if self._handle:
            memory.close(self._handle)
            self._handle = None


# ---- state detection -----------------------------------------------------
def _band_hits(rect, y_frac) -> int:
    ox, oy, w, h = rect
    y = oy + int(y_frac * h)
    x0, x1 = ox + int(_HDR_X0 * w), ox + int(_HDR_X1 * w)
    step = max(1, (x1 - x0) // _HDR_SAMPLES)
    return sum(
        all(abs(screen.pixel(x, y)[k] - _LIME[k]) <= _HDR_TOL for k in range(3))
        for x in range(x0, x1, step)
    )


def _arrow_hits(rect, x0_frac, x1_frac, y_frac) -> int:
    """Count orange (nav-arrow) pixels along a horizontal segment."""
    ox, oy, w, h = rect
    y = oy + int(y_frac * h)
    hits = 0
    for k in range(_ARROW_SAMPLES):
        xf = x0_frac + (x1_frac - x0_frac) * k / (_ARROW_SAMPLES - 1)
        c = screen.pixel(ox + int(xf * w), y)
        if all(abs(c[j] - _ARROW_RGB[j]) <= _ARROW_TOL[j] for j in range(3)):
            hits += 1
    return hits


def _row_dark(rect, y_frac) -> bool:
    """True if the menu row is the SELECTED (black-filled) one. Samples the row
    FILL at several off-centre x (0.33/0.36/0.64/0.67) — never the centred white
    text, which would invert the reading — and decides by majority."""
    ox, oy, w, h = rect
    y = oy + int(y_frac * h)
    dark = 0
    for xf in (0.33, 0.36, 0.64, 0.67):
        r, g, b = screen.pixel(ox + int(xf * w), y)
        if r + g + b < 240:
            dark += 1
    return dark >= 3  # selected row = black fill; unselected = white


def detect(rect) -> tuple[str, dict]:
    """Return (state_name, detail). States: autoshow_confirm / buy_car /
    added_to_garage / car_collection / unknown."""
    with screen.frame_session(rect):
        for name, y in _HDR.items():
            if _band_hits(rect, y) >= _HDR_MIN_HITS:
                detail = {}
                rows = _ROWS.get(name)
                if rows:
                    detail["first_selected"] = _row_dark(rect, rows[0])
                    detail["second_selected"] = _row_dark(rect, rows[1])
                return name, detail
        ox, oy, w, h = rect
        arrows = all(_arrow_hits(rect, x0, x1, yf) >= _ARROW_MIN_HITS for x0, x1, yf in _ARROW_SEGS)
        return ("car_collection", {}) if arrows else ("unknown", {})


# ---- runner --------------------------------------------------------------
def run(cfg: dict, *, starting_credits: int, max_purchases: int, reserve_percent: float,
        stop, on_log, on_status) -> None:
    """Buyer loop. on_status(dict) publishes live stats to the UI."""
    log = on_log or (lambda m: None)
    backend = inputs.make_backend(cfg)
    poll = max(0.02, float(cfg.get("buyer_poll_s", 0.05)))
    next_key = cfg.get("buyer_next_key", "right")
    purchase_key = cfg.get("buyer_purchase_key", "space")
    confirm_key = cfg.get("buyer_confirm_key", "enter")
    stop_on_ocr_fail = cfg.get("buyer_stop_on_ocr_fail", True)
    templates = _load_price_templates()
    have = "".join(sorted(templates)) if templates else ""
    if len(have) < 10:
        log(f"[buyer] price OCR digits learned: '{have or 'none'}' — run "
            "tools/calibrate_price.py on a Buy Car popup to learn 0-9.")

    # Live credit balance source: 'memory' (read-only, sees CR even behind a
    # menu), 'ocr' (reads the yellow CR HUD when visible), or 'manual'.
    credit_mode = cfg.get("buyer_credits_mode", "manual")
    source = CreditSource(cfg, credit_mode, starting_credits, log)

    def live_credits() -> int | None:
        try:
            return source.read()
        except Exception:
            return None

    live = live_credits()
    if live is not None:
        starting_credits = live
        log(f"[buyer] credits via {source.mode} read={live}")
    elif source.mode != "manual":
        log(f"[buyer] {source.mode} read unavailable at start; using manual starting credits.")

    reserve = max(0.0, starting_credits * reserve_percent / 100.0)
    stats = {
        "running": True, "state": "starting", "cars_bought": 0,
        "credits_start": starting_credits, "credits_remaining_estimate": starting_credits,
        "last_price_detected": None, "discount_percent_detected": None,
        "stop_reason": None, "credits_source": source.mode,
    }

    def status(state: str) -> None:
        stats["state"] = state
        if on_status:
            on_status(dict(stats))

    def halt(reason: str) -> None:
        stats["stop_reason"] = reason
        log(f"[buyer] stop: {reason}")

    pending_price = None
    pending_bal_before = None
    unknown_since = None
    last_action = 0.0
    last_logged = None
    debug_saved = 0
    warned_unbounded = [False]
    buycar_dbg = [False]
    try:
        while stop is None or not stop.is_set():
            screen.check_failsafe()
            win = window.select_game_window(cfg)
            if not window.is_foreground(win):
                status("paused")
                time.sleep(0.2)
                continue
            rect = win[3] if win else (0, 0, *screen.size())
            name, detail = detect(rect)
            now = time.time()
            if name != last_logged:
                log(f"[buyer] detected state={name}")
                last_logged = name
            if name != "unknown":
                unknown_since = None
            if now - last_action < 0.35:  # let FH6 animate between popups
                time.sleep(poll)
                continue

            if name == "car_collection":
                status("car_collection")
                if max_purchases and stats["cars_bought"] >= max_purchases:
                    halt("max purchases reached")
                    break
                log(f"[buyer] state=car_collection -> purchase -> {purchase_key}")
                backend.tap(purchase_key)
                last_action = time.time()

            elif name == "autoshow_confirm":
                status("autoshow_confirm")
                if detail.get("first_selected"):  # "No" selected
                    log("[buyer] autoshow confirm: No selected -> down+enter")
                    backend.tap("down")
                    time.sleep(0.25)
                else:
                    log("[buyer] autoshow confirm: Yes selected -> enter")
                backend.tap(confirm_key)
                last_action = time.time()

            elif name == "buy_car":
                status("buy_car")
                if not buycar_dbg[0]:
                    buycar_dbg[0] = True
                    try:
                        screen.save_png(str(Path.home() / ".cruise" / "buycar_debug.png"), rect)
                        log("[buyer] DEBUG saved buycar_debug.png (price-line calibration).")
                    except Exception as e:
                        log(f"[buyer] buycar debug failed: {e}")
                bal_before = live_credits()  # live balance (memory), else None
                price = read_price(rect, templates)  # OCR; None without templates
                if price is not None:
                    stats["last_price_detected"] = price
                if bal_before is not None:
                    # Live balance known: guard on it directly. If the price is
                    # also OCR'd, pre-check exactly; else require a min floor so
                    # we never buy when nearly at the reserve.
                    need = price if price is not None else int(cfg.get("buyer_min_price", 0))
                    if bal_before - need < reserve:
                        halt("insufficient credits")
                        break
                elif price is not None:
                    if stats["credits_remaining_estimate"] - price < reserve:
                        halt("insufficient credits")
                        break
                else:
                    # No price/credit figures available: buy anyway, bounded by
                    # MAX PURCHASES (credit tracking is best-effort).
                    if not max_purchases and not warned_unbounded[0]:
                        warned_unbounded[0] = True
                        log("[buyer] no price/credit read -> set MAX PURCHASES to bound spending.")
                log(f"[buyer] buy car price={price if price is not None else '?'}"
                    + (f" balance={bal_before}" if bal_before is not None else ""))
                if detail.get("second_selected"):  # cursor on "Buy Vouchers"
                    backend.tap("up")
                    time.sleep(0.25)
                pending_price = price          # may be None -> derived after buy
                pending_bal_before = bal_before
                backend.tap(confirm_key)
                last_action = time.time()

            elif name == "added_to_garage":
                status("added_to_garage")
                stats["cars_bought"] += 1
                source.on_purchase()           # lock the credit address (memory)
                live_now = live_credits()
                if live_now is not None:
                    if pending_price is None and pending_bal_before is not None:
                        pending_price = pending_bal_before - live_now
                    if pending_price is not None and pending_price > 0:
                        stats["last_price_detected"] = pending_price
                    stats["credits_remaining_estimate"] = live_now
                elif pending_price is not None:
                    stats["credits_remaining_estimate"] -= pending_price
                log(f"[buyer] bought count={stats['cars_bought']} "
                    f"price={pending_price if pending_price is not None else '?'} "
                    f"remaining_est={stats['credits_remaining_estimate']}")
                pending_price = None
                pending_bal_before = None
                backend.tap(confirm_key)  # OK -> FH6 auto-advances to the next car
                # No next-arrow: pressing it here would skip a car each cycle.
                last_action = time.time()
                if max_purchases and stats["cars_bought"] >= max_purchases:
                    halt("max purchases reached")
                    break
                if live_now is not None and live_now <= reserve:
                    halt("insufficient credits")
                    break

            else:  # unknown
                status("unknown")
                # Save a fresh debug frame each time we ENTER unknown (up to a
                # few), so an unrecognized popup after Space is captured too.
                if unknown_since is None and debug_saved < 6:
                    try:
                        with screen.frame_session(rect):
                            hdrs = {k: _band_hits(rect, y) for k, y in _HDR.items()}
                            arr = [_arrow_hits(rect, *seg) for seg in _ARROW_SEGS]
                        dbg = str(Path.home() / ".cruise" / f"buyer_debug_{debug_saved}.png")
                        screen.save_png(dbg, rect)
                        log(f"[buyer] DEBUG#{debug_saved} {dbg}; arrows={arr} hdr_hits={hdrs}")
                        debug_saved += 1
                    except Exception as e:
                        log(f"[buyer] DEBUG capture failed: {e}")
                if unknown_since is None:
                    unknown_since = now
                elif now - unknown_since > 10.0:
                    halt("unknown state")
                    break
            time.sleep(poll)
    except screen.FailSafeException:
        halt("failsafe (mouse in corner)")
    finally:
        backend.close()
        source.close()
        stats["running"] = False
        status("stopped")
