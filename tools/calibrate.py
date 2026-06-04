"""Calibration tool: continuously displays cursor position + pixel RGB color.

Usage:
    python calibrate.py

Place the cursor on:
  - a characteristic MENU pixel (always present in the menu, absent in-game)
  - the "Start race event" button
Note the coords (x, y) and the RGB color, then report them in config.json.
Ctrl+C to quit.
"""
import time

import pyautogui


def main() -> None:
    print("Calibration — bouge la souris, lis x/y/RGB. Ctrl+C pour quitter.\n")
    try:
        while True:
            x, y = pyautogui.position()
            r, g, b = pyautogui.pixel(x, y)
            print(f"\rx={x:5d} y={y:5d}  rgb=({r:3d},{g:3d},{b:3d})", end="", flush=True)
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nStop.")


if __name__ == "__main__":
    main()
