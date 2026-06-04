"""Controlled calibration run: starts the race, drives, detects the results
screen by its lime bar, captures the real RGB color, releases W cleanly.

Prerequisite: pre-race MENU displayed (Start Race Event highlighted).
Usage: py calibrate_results.py
"""
import time
from pathlib import Path

import pyautogui
import pydirectinput

pyautogui.FAILSAFE = True
pydirectinput.PAUSE = 0.0

LIME_ROW_FRAC = 0.227   # y of the lime bar
MAX_RACE_S = 120
POLL = 0.7


def is_limeish(px) -> bool:
    r, g, b = px
    return g > 150 and 110 < r < 235 and b < 120 and g >= r - 10


def main() -> None:
    w, h = pyautogui.size()
    y = int(LIME_ROW_FRAC * h)
    x0, x1 = int(0.20 * w), int(0.80 * w)

    print("Demarrage course dans 4s...")
    time.sleep(4)
    pydirectinput.press("enter")          # Start Race Event
    time.sleep(6)                          # loading
    pydirectinput.keyDown("w")             # accelerate
    print("Roule. Attente ecran resultats...")

    t0 = time.time()
    try:
        while time.time() - t0 < MAX_RACE_S:
            img = pyautogui.screenshot()
            run = []
            for x in range(x0, x1, 8):
                if is_limeish(img.getpixel((x, y))):
                    run.append(x)
            if len(run) >= 20:            # wide continuous lime band = results
                pydirectinput.keyUp("w")
                out = Path(__file__).with_name("results_calib.png")
                img.save(out)
                xa, xb = run[0], run[-1]
                samples = [run[len(run) // 4], run[len(run) // 2], run[3 * len(run) // 4]]
                lines = [f"ECRAN RESULTATS detecte. barre lime y={y}, x de {xa} a {xb}"]
                for sx in samples:
                    lines.append(f"  x={sx} ({sx / w:.3f}) rgb={img.getpixel((sx, y))}")
                lines.append(f"capture: {out}")
                report = "\n".join(lines)
                print(report)
                Path(__file__).with_name("results_calib.txt").write_text(report, encoding="utf-8")
                return
            time.sleep(POLL)
        print("Timeout: ecran resultats non detecte.")
    finally:
        pydirectinput.keyUp("w")


if __name__ == "__main__":
    main()
