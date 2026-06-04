"""Verifies that screen capture works on top of the game.

Usage:
    py test_capture.py
After launch, switch IMMEDIATELY to FH6 (foreground).
After the countdown, the script captures the screen -> capture_test.png + center RGB.
If the image shows the game (not black) -> pixel detection OK.
"""
import time
from pathlib import Path

import pyautogui

DELAY = 5

print(f"Bascule sur FH6 maintenant. Capture dans {DELAY}s...")
time.sleep(DELAY)

w, h = pyautogui.size()
img = pyautogui.screenshot()
out = Path(__file__).with_name("capture_test.png")
img.save(out)

cx, cy = w // 2, h // 2
print(f"Resolution: {w}x{h}")
print(f"Pixel centre ({cx},{cy}) = {img.getpixel((cx, cy))}")
print(f"Image sauvee: {out}")
print("Ouvre l'image: si tu vois le jeu -> OK. Si noir -> passe en fenetre borderless.")
