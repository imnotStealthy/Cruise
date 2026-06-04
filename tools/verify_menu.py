"""Verifies state detection on the current screen.

Usage:
    py verify_menu.py
Display the SCREEN to test (results OR menu), switch to FH6, wait for the countdown.
For each state: resolved coord, RGB read, match? + global detect_state() verdict.
"""
import sys
import time
from pathlib import Path

import pyautogui

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root
from bot import detect_state, detection_rect, display_status, load_config, resolve

DELAY = 5

print(f"Affiche l'ecran a tester. Lecture dans {DELAY}s...")
time.sleep(DELAY)

cfg = load_config()
disp = display_status(cfg)
ox, oy, w, h = detection_rect(cfg)
print(f"Display: {disp}")
print(f"Zone detection (x,y,w,h) = ({ox},{oy},{w},{h})\n")

for state in cfg["states"]:
    print(f"== etat '{state['name']}' (mode {state.get('match_mode', 'all')}) ==")
    for i, p in enumerate(state["pixels"]):
        rx, ry = ox + resolve(p["x"], w), oy + resolve(p["y"], h)
        got = pyautogui.pixel(rx, ry)
        tol = p.get("tol", 20)
        ok = all(abs(got[k] - p["rgb"][k]) <= tol for k in range(3))
        print(f"  pixel[{i}] @({rx},{ry}) attendu={p['rgb']} lu={got} -> {'MATCH' if ok else 'NO'}")

active = detect_state(cfg, (ox, oy, w, h))
print(f"\ndetect_state() = {active['name'] if active else None}")
