"""Times an FH6 race (read-only). Time calibration.

Samples the state (config.json: menu/results/prerace_menu) via bot.detect_state and
timestamps each transition. No keyboard/mouse input, no modification.
Goal: find out how long a race takes and WHETHER it finishes.

Usage: python tools/time_race.py [max_duration_s] [poll_s]
Ctrl+C to stop. Summary + log -> tools/time_race.log
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bot  # noqa: E402

MAX_S = float(sys.argv[1]) if len(sys.argv) > 1 else 300.0
POLL_S = float(sys.argv[2]) if len(sys.argv) > 2 else 0.5
LOG = Path(__file__).with_name("time_race.log")


def main() -> None:
    cfg = bot.load_config()
    bot.select_game_window(cfg)
    rect = bot.detection_rect(cfg)

    t0 = time.time()
    prev = "__init__"
    rows: list[str] = []

    def emit(line: str) -> None:
        rows.append(line)
        print(line, flush=True)

    emit(f"# start {datetime.now():%H:%M:%S} rect={rect} max={MAX_S}s poll={POLL_S}s")
    try:
        while time.time() - t0 < MAX_S:
            state = bot.detect_state(cfg, rect)
            name = state["name"] if state else "driving"
            if name != prev:
                dt = time.time() - t0
                emit(f"{dt:8.2f}s  {prev:>13} -> {name}")
                prev = name
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        emit(f"# interrupted t={time.time() - t0:.2f}s")
    emit(f"# end {datetime.now():%H:%M:%S} total={time.time() - t0:.2f}s")
    LOG.write_text("\n".join(rows), encoding="utf-8")


if __name__ == "__main__":
    main()
