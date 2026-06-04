"""Controlled test: starts the race, drives, opens the pause menu (ESC), captures the screen.
Usage: py test_pause.py   (FH6 on the pre-race menu, in the foreground)
"""
import sys
import time
from pathlib import Path

import pyautogui
import pydirectinput as p

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # project root
import bot

p.PAUSE = 0.0
cfg = bot.load_config()
print("focus:", bot.focus_game_window(cfg))
time.sleep(0.6)
try:
    print("Start Race Event")
    p.press("enter")
    time.sleep(0.3)
    p.keyDown("w")  # held during loading + countdown -> launch at GO
    print("hold W ~15s (chargement + 3-2-1-GO + roule)")
    time.sleep(15)
    p.keyUp("w")
    print("ESC -> menu pause")
    p.press("esc")
    time.sleep(2.0)
    img = pyautogui.screenshot()
    out = Path(__file__).with_name("pause_menu.png")
    img.save(out)
    print("saved", out)
finally:
    p.keyUp("w")
