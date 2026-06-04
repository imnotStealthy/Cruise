"""Diagnostic: why isn't the 'results' state detected?

Run this WHILE the Forza results screen is on display. Uses the SAME window/
screen modules as the bot, so coordinates match exactly (window-relative).

Reports, for every config state: the actual RGB read at each pixel vs expected,
and whether the state would match. Then band-scans y=0.227 to locate the real
lime bar. Read-only — sends no input.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot
import screen
import window


def main() -> int:
    delay = 3
    print(f"Switch to the Forza RESULTS screen. Sampling in {delay}s...")
    time.sleep(delay)

    cfg = bot.load_config()
    rect = window.detection_rect(cfg)
    ox, oy, w, h = rect
    win = window.select_game_window(cfg)
    print(f"window found={bool(win)} rect=(x={ox},y={oy},w={w},h={h})")
    if win:
        st = window.display_status(cfg)
        print(f"fullscreen={st['fullscreen']}")

    with screen.dc_session():
        for state in cfg["states"]:
            checks = []
            print(f"\n[{state['name']}] match_mode={state.get('match_mode','all')} "
                  f"guard={state.get('guard', False)}")
            for p in state["pixels"]:
                px = ox + bot.resolve(p["x"], w)
                py = oy + bot.resolve(p["y"], h)
                got = screen.pixel(px, py)
                exp = p["rgb"]
                tol = p.get("tol", 20)
                ok = all(abs(got[k] - exp[k]) <= tol for k in range(3))
                checks.append(ok)
                print(f"  x={p['x']} y={p['y']} -> px=({px},{py}) "
                      f"got={got} exp={exp} tol={tol} {'OK' if ok else 'NO'}")
            mode = state.get("match_mode", "all")
            matched = all(checks) if mode == "all" else any(checks)
            print(f"  => {state['name']} MATCHES" if matched else f"  => {state['name']} no")

    # Band-scan y=0.227: where is the real lime bar (g high, r/b lower)?
    y = oy + int(0.227 * h)
    lime = []
    with screen.dc_session():
        for x in range(ox + int(0.10 * w), ox + int(0.90 * w), max(1, w // 200)):
            r, g, b = screen.pixel(x, y)
            if g > 150 and r < 235 and b < 130 and g >= r - 15:
                lime.append(((x - ox) / w, (r, g, b)))
    print(f"\nlime band @ y=0.227: {len(lime)} pts")
    for fx, c in lime[:: max(1, len(lime) // 8 or 1)]:
        print(f"  x_frac={fx:.3f} rgb={c}")
    if lime:
        xs = [fx for fx, _ in lime]
        print(f"  lime spans x_frac {min(xs):.3f}..{max(xs):.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
