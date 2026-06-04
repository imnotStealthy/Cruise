"""Samples the lime bar on the CURRENT screen (results). Read-only."""
import time
from pathlib import Path

import pyautogui

time.sleep(2)
w, h = pyautogui.size()
y = int(0.227 * h)
run = []
for x in range(int(0.20 * w), int(0.80 * w), 6):
    r, g, b = pyautogui.pixel(x, y)
    if g > 150 and 110 < r < 235 and b < 120 and g >= r - 10:
        run.append((x, (r, g, b)))

img = pyautogui.screenshot()
out = Path(__file__).with_name("results_calib.png")
img.save(out)

lines = [f"res={w}x{h} y={y} lime_pts={len(run)}"]
if run:
    for x, c in run[:: max(1, len(run) // 5)]:
        lines.append(f"  x={x} ({x / w:.3f}) rgb={c}")
report = "\n".join(lines)
print(report)
Path(__file__).with_name("results_calib.txt").write_text(report, encoding="utf-8")
