"""Cruise — Forza Horizon 6 AFK loop.

  1. Holds acceleration (keyboard or gamepad) -> the vehicle moves forward.
  2. Samples pixels (relative to the FH6 window) to recognize a STATE
     (config: states): pre-race menu, results screen, pause menu.
  3. Recognized state -> sends its keys (Start Race Event / Restart) or pauses
     (guard). Holds acceleration during the countdown. Loop.

Window/display detection: window.py. Inputs: inputs.py.
Safety: mouse top-left corner -> failsafe. Clean stop: Ctrl+C.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import screen
import inputs
import telemetry as telemetry_mod
import window
# re-export for consumers (server, tools): bot.detection_rect, etc.
from window import (  # noqa: F401
    detection_rect,
    display_status,
    focus_game_window,
    game_client_rect,
    is_foreground,
    select_game_window,
)

_BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
# Bundle dir (PyInstaller _MEIPASS) holds the default config shipped inside the
# exe -> a fresh Cruise.exe with no sibling files seeds itself on first run.
_MEI = Path(getattr(sys, "_MEIPASS", _BASE))
DATA_DIR = Path.home() / ".cruise"
CONFIG_PATH = DATA_DIR / "config.json"

# Stuck-detection sampling grid: fractional points over the lower-center of the
# FH6 window (road + scenery + car), avoiding HUD corners. When driving these
# pixels change every frame; a stuck car (hit a vehicle/wall) freezes them.
MOTION_POINTS = [(x, y) for y in (0.55, 0.70, 0.85) for x in (0.30, 0.45, 0.60, 0.75)]

def _seed_file(path: Path) -> None:
    """Create `path` from the copy bundled in the exe if it's missing -> Cruise.exe
    distributed alone still works on first launch (no manual config needed)."""
    if path.exists():
        return
    src = _MEI / path.name
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if src.exists() and src.resolve() != path.resolve():
            path.write_bytes(src.read_bytes())
    except OSError:
        pass


def load_config() -> dict:
    _seed_file(CONFIG_PATH)  # first run with only Cruise.exe -> generate config.json
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def resolve(coord: float, size: int) -> int:
    """Fractional coord (0<coord<=1) -> pixel; otherwise absolute pixel."""
    return int(coord * size) if 0.0 < coord <= 1.0 else int(coord)


def pixel_matches(p: dict, rect: tuple[int, int, int, int]) -> bool:
    ox, oy, w, h = rect
    got = screen.pixel(ox + resolve(p["x"], w), oy + resolve(p["y"], h))
    tol = p.get("tol", 20)
    rgb = p["rgb"]
    return all(abs(got[k] - rgb[k]) <= tol for k in range(3))


def band_matches(band: dict, rect: tuple[int, int, int, int]) -> bool:
    """True if a horizontal line at band["y"] contains >= min_hits pixels of the
    target color across [x0, x1]. Robust to text/numbers/edges overlaid on the
    bar and to layout shifts (counts the colour, not exact points) — unlike a few
    fixed points where one landing on text (e.g. the FH6 results lime header)
    breaks an all-match."""
    ox, oy, w, h = rect
    y = oy + resolve(band["y"], h)
    x0 = ox + resolve(band["x0"], w)
    x1 = ox + resolve(band["x1"], w)
    rgb = band["rgb"]
    tol = band.get("tol", 30)
    samples = max(1, int(band.get("samples", 60)))
    step = max(1, (x1 - x0) // samples)
    hits = sum(
        all(abs(screen.pixel(x, y)[k] - rgb[k]) <= tol for k in range(3))
        for x in range(x0, x1, step)
    )
    return hits >= int(band.get("min_hits", 20))


def state_active(state: dict, rect: tuple[int, int, int, int]) -> bool:
    checks = [pixel_matches(p, rect) for p in state.get("pixels", [])]
    if "band" in state:
        checks.append(band_matches(state["band"], rect))
    if not checks:
        return False
    return all(checks) if state.get("match_mode", "all") == "all" else any(checks)


def selected_menu_keys(state: dict, rect: tuple[int, int, int, int]) -> list[dict] | None:
    """Return navigation keys needed to move the current menu selection to target.

    FH6 wraps menu navigation, so a fixed "up xN" sequence cannot normalize every
    starting row. The selected row has a dark fill; unselected rows are white.
    """
    menu = state.get("selected_menu")
    if not menu:
        return []
    ox, oy, w, h = rect
    x = ox + resolve(menu.get("x", 0.18), w)
    dark_max = int(menu.get("dark_max", 120))
    with screen.dc_session():
        for row in menu.get("rows", []):
            y = oy + resolve(row["y"], h)
            rgb = screen.pixel(x, y)
            if sum(rgb) / 3 <= dark_max:
                return row.get("keys", [])
    return None if menu.get("required", True) else []


def detect_state(cfg: dict, rect: tuple[int, int, int, int] | None = None) -> dict | None:
    if rect is None:
        rect = detection_rect(cfg)
    with screen.dc_session():  # a single screen DC for all pixels in the poll
        for state in cfg["states"]:
            if state_active(state, rect):
                return state
    return None


def _sleep(duration: float, stop) -> None:
    """Sleeps in 0.1s slices while watching stop (Event or None)."""
    duration = max(0.0, float(duration))
    end = time.time() + duration
    while time.time() < end:
        if stop is not None and stop.is_set():
            return
        time.sleep(min(0.1, end - time.time()))


def _duration(value, default: float, minimum: float = 0.0) -> float:
    try:
        duration = float(value)
    except (TypeError, ValueError):
        duration = default
    return max(minimum, duration)


def _ratio(value, default: float, minimum: float = 0.0, maximum: float = 1.0) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        ratio = default
    return max(minimum, min(maximum, ratio))


def _launch_feather(backend, duration: float, stop, hold: float = 0.45, lift: float = 0.3) -> None:
    """Pulse the throttle for `duration` seconds (gentle launch) to limit wheelspin
    and torque-steer at GO when Traction Control is off. Leaves the throttle held."""
    end = time.time() + duration
    while time.time() < end:
        if stop is not None and stop.is_set():
            return
        backend.hold_accelerate()
        _sleep(min(hold, end - time.time()), stop)
        if time.time() >= end or (stop is not None and stop.is_set()):
            break
        backend.release_accelerate()
        _sleep(min(lift, end - time.time()), stop)
    backend.hold_accelerate()


def run(cfg: dict, max_cycles: int = 0, stop=None, pause=None, on_log=None, on_status=None) -> int:
    """Controllable AFK loop.

    cfg        : loaded config (load_config()).
    max_cycles : number of laps (Start Race Event) before stopping (0 = unlimited).
    stop       : optional threading.Event for a clean external stop.
    on_log     : callback(str) for logging (default print).
    on_status  : callback(state:str, cycles:int) for the UI.
    Returns the number of laps completed.
    """
    log = on_log or print
    steer = cfg.get("steer_key")
    cycles = 0

    def status(s: str) -> None:
        if on_status:
            on_status(s, cycles)

    backend = inputs.make_backend(cfg)
    start_delay = _duration(cfg.get("start_delay_s"), 0.0)
    loop_poll = _duration(cfg.get("loop_poll_s"), 1.0, 0.1)
    status("starting")
    focused = window.focus_game_window(cfg)
    log(f"Input: {backend.name}. Game focus: {'ok' if focused else 'window not found'}.")
    if start_delay > 0:
        log(f"Starting in {start_delay}s...")
        _sleep(start_delay, stop)
    else:
        _sleep(0.25, stop)  # small settle so the focus takes effect
    log(f"AFK running (max_laps={max_cycles or 'inf'}). Mouse top-left = failsafe.")

    guard = cfg.get("pause_when_unfocused", True)
    # How many times to tap the resume key to leave a pause menu before giving up
    # (alt-tab back: the first tap can miss while focus is still settling).
    menu_resume_tries = max(1, int(cfg.get("menu_resume_tries", 3)))
    paused = False
    menu_resumes = 0

    # Stuck detection / collision recovery (config-tunable, on by default).
    recovery = cfg.get("recovery_enabled", True)
    # "rewind": tap the game's Rewind (snaps the car back on track) — best with
    # auto-steering on. "maneuver": blind reverse + steer.
    recover_mode = cfg.get("recover_mode", "rewind")
    rewind_wait = _duration(cfg.get("rewind_wait_s"), 2.0, 0.2)  # let the rewind play out
    stuck_after = _duration(cfg.get("stuck_after_s"), 2.5, 0.5)
    motion_tol = int(cfg.get("stuck_motion_tol", 90))      # signature delta below = "no motion"
    recover_cooldown = _duration(cfg.get("recover_cooldown_s"), 2.0)
    reverse_s = _duration(cfg.get("recover_reverse_s"), 1.0)
    steer_s = _duration(cfg.get("recover_steer_s"), 0.8)
    # Telemetry-gated stuck: with FH6 "Data Out" on, only rewind when actually
    # stopped (speed ~0) -> a jump or off-road run (still fast) no longer triggers
    # a false rewind. Falls back to the visual motion check if no packets arrive.
    stuck_speed = _duration(cfg.get("stuck_speed_kmh"), 5.0)  # km/h below = "stopped"
    telem = None
    if cfg.get("telemetry_enabled", True):
        tel_host = cfg.get("telemetry_host", "127.0.0.1")
        telem = telemetry_mod.shared(cfg.get("telemetry_port", 5300), tel_host)
        if telem:
            log(f"Telemetry: listening udp/{tel_host}:{telem.port} (enable FH6 Data Out -> {tel_host}:{telem.port}).")
        else:
            log("Telemetry: cannot bind port; visual stuck detection only.")
    last_sig = None
    stuck_since = time.time()
    recover_dir = "right"   # alternates each recovery to try both sides
    last_recover = 0.0

    # Optional throttle modulation: brief periodic throttle lift so the in-game
    # braking assist / engine braking slows the car for corners (off by default).
    modulate = bool(cfg.get("throttle_modulation", False))
    throttle_hold = _duration(cfg.get("throttle_hold_s"), 2.0, 0.3)
    throttle_lift = _duration(cfg.get("throttle_lift_s"), 0.4, 0.05)
    last_lift = time.time()

    # Optional launch ease-in: feather the throttle for the first seconds of a
    # start/restart so the car doesn't wheelspin/veer at GO (Traction Control off).
    launch_ease = bool(cfg.get("launch_ease", False))
    launch_ease_s = _duration(cfg.get("launch_ease_s"), 4.0, 0.5)

    # Manual gearbox assist: if telemetry shows the car sitting at the limiter,
    # tap the configured upshift key. Automatic shifting usually changes gear
    # before this dwell timer expires, so no explicit mode toggle is needed.
    shift_assist = bool(cfg.get("manual_shift_assist", True))
    shift_up_ratio = _ratio(cfg.get("shift_up_rpm_ratio"), 0.90, 0.5, 1.0)
    shift_detect_s = _duration(cfg.get("shift_detect_s"), 0.25, 0.1)
    shift_cooldown = _duration(cfg.get("shift_cooldown_s"), 0.9, 0.2)
    shift_min_speed = _duration(cfg.get("shift_min_speed_kmh"), 8.0)
    if backend.name == "gamepad":
        shift_up_key = cfg.get("gamepad_shift_up_key", "b")
    else:
        shift_up_key = cfg.get("shift_up_key", "e")
    high_rpm_since = 0.0
    high_rpm_gear = None
    last_shift = 0.0

    def reset_stuck() -> None:
        nonlocal last_sig, stuck_since, last_lift
        last_sig = None
        stuck_since = time.time()
        last_lift = time.time()

    def maybe_shift_up() -> None:
        nonlocal high_rpm_since, high_rpm_gear, last_shift
        if not (shift_assist and telem):
            return
        fresh, race_on, speed_kmh, gear, current_rpm, max_rpm = telem.drivetrain()
        if not (fresh and race_on and max_rpm > 0.0 and speed_kmh >= shift_min_speed):
            high_rpm_since = 0.0
            high_rpm_gear = None
            return
        if current_rpm / max_rpm < shift_up_ratio:
            high_rpm_since = 0.0
            high_rpm_gear = None
            return
        now = time.time()
        if high_rpm_since <= 0.0 or gear != high_rpm_gear:
            high_rpm_since = now
            high_rpm_gear = gear
            return
        if now - high_rpm_since >= shift_detect_s and now - last_shift >= shift_cooldown:
            log(f"Manual shift assist -> '{shift_up_key}' ({current_rpm / max_rpm:.0%} rpm).")
            backend.tap(shift_up_key)
            last_shift = now
            high_rpm_since = now

    def drive_wait(duration: float) -> None:
        end = time.time() + max(0.0, duration)
        while time.time() < end:
            if stop is not None and stop.is_set():
                return
            maybe_shift_up()
            _sleep(min(0.1, end - time.time()), stop)

    def enter_pause(reason: str) -> None:
        nonlocal paused
        backend.release_accelerate()
        backend.release_steer()
        if not paused:
            log(f"Paused: {reason}.")
            paused = True
        status("paused")
        reset_stuck()

    try:
        while stop is None or not stop.is_set():
            screen.check_failsafe()  # mouse in a corner -> emergency stop

            # Manual pause (UI button): release inputs and idle until resumed.
            if pause is not None and pause.is_set():
                backend.release_accelerate()
                backend.release_steer()
                if not paused:
                    log("Paused (manual).")
                    paused = True
                status("paused")
                reset_stuck()
                _sleep(loop_poll, stop)
                continue

            # FH6 window computed once per loop (foreground + detection zone)
            win = window.select_game_window(cfg)

            # Guard 1: FH6 not in the foreground (alt-tab / other app) -> send
            # NOTHING (avoids sending W/X to another window).
            if guard and not window.is_foreground(win):
                enter_pause("Forza Horizon 6 not focused (alt-tab)")
                menu_resumes = 0  # fresh resume attempts each time focus returns
                _sleep(loop_poll, stop)
                continue

            rect = win[3] if win else detection_rect(cfg)
            state = detect_state(cfg, rect)

            # Guard 2: "guard" screen (pause menu / dashboard).
            if state is not None and state.get("guard", False):
                rk = state.get("resume_key")
                # Auto-resume: tap the resume key (esc/B) to close the menu and
                # resume the race, retrying up to menu_resume_tries (the first tap
                # often misses right after an alt-tab while focus is settling).
                # Re-detection each loop stops as soon as the menu is gone; the
                # cap bounds damage if it's actually a stuck dashboard.
                if rk and menu_resumes < menu_resume_tries:
                    log(f"Pause menu detected -> '{rk}' to resume ({menu_resumes + 1}/{menu_resume_tries}).")
                    backend.tap(rk)
                    menu_resumes += 1
                    status("paused")
                    _sleep(1.2, stop)
                    continue
                enter_pause(f"'{state['name']}' menu")
                _sleep(loop_poll, stop)
                continue

            if paused:
                log("Resumed.")
                paused = False
            menu_resumes = 0

            if state is not None:
                reset_stuck()  # menus/results/countdown are not "racing"
                backend.release_accelerate()
                backend.release_steer()
                menu_keys = selected_menu_keys(state, rect)
                if menu_keys is None:
                    log(f"[{state['name']}] selected menu row not detected; waiting.")
                    _sleep(loop_poll, stop)
                    continue
                steps = menu_keys + state.get("keys", [])
                keys = [k["key"] for k in steps]
                log(f"[{state['name']}] -> {keys}")
                status(state["name"])
                for step in steps:
                    backend.tap(step["key"])
                    _sleep(_duration(step.get("wait"), 0.5), stop)
                # 1 lap counts only on the state marked count_lap (Start Race Event),
                # not on Restart Event -> avoids double counting per loop.
                if state.get("count_lap", False):
                    cycles += 1
                    status(state["name"])
                    if max_cycles and cycles >= max_cycles:
                        log(f"Max laps reached ({cycles}). Clean stop.")
                        break
                # Hold the accelerator during loading + 3-2-1-GO countdown
                # -> the car launches right at GO (instead of staying still).
                post_wait = _duration(state.get("post_wait_s", cfg.get("post_restart_wait_s")), 8.0)
                if state.get("hold_during_wait", False):
                    if steer:
                        backend.hold_steer()
                    if launch_ease:
                        # feather the launch, then full throttle for the remainder
                        ease = min(launch_ease_s, post_wait)
                        _launch_feather(backend, ease, stop)
                        drive_wait(post_wait - ease)
                    else:
                        backend.hold_accelerate()
                        drive_wait(post_wait)
                else:
                    _sleep(post_wait, stop)
                reset_stuck()  # fresh launch -> reset stuck/modulation timers
                continue

            backend.reassert_accelerate()  # re-press each poll -> stray W bump can't desync
            if steer:
                backend.hold_steer()
            status("racing")

            # Stuck/collision detection: the car hit a vehicle/wall -> rewind (or
            # back up and steer). With telemetry we know real speed, so a jump or
            # off-road run (still fast) can't be mistaken for stuck; otherwise we
            # fall back to "the sampled scene stopped changing".
            if recovery:
                now = time.time()
                fresh, race_on, speed_kmh = telem.snapshot() if telem else (False, False, 0.0)
                if fresh:
                    if (not race_on) or speed_kmh > stuck_speed:
                        stuck_since = now  # moving (incl. airborne/off-road) or not racing
                else:
                    sig = screen.motion_signature(rect, MOTION_POINTS)
                    if screen.signature_delta(sig, last_sig) > motion_tol:
                        stuck_since = now  # scene moved -> not stuck
                    last_sig = sig
                if now - stuck_since >= stuck_after and now - last_recover >= recover_cooldown:
                    status("recovering")
                    if recover_mode == "rewind":
                        log("Stuck detected -> rewind." + (f" ({speed_kmh:.0f} km/h)" if fresh else ""))
                        backend.rewind()
                        _sleep(rewind_wait, stop)  # let the rewind snap the car back
                        backend.hold_accelerate()  # resume throttle on the track
                    else:
                        log(f"Stuck detected -> recovery (reverse + steer {recover_dir}).")
                        backend.recover(recover_dir, reverse_s, steer_s)
                        recover_dir = "left" if recover_dir == "right" else "right"
                    last_recover = time.time()
                    reset_stuck()

            # Throttle modulation: every throttle_hold_s, lift the gas briefly so
            # the braking assist can scrub speed into corners. Opt-in.
            if modulate and time.time() - last_lift >= throttle_hold:
                backend.release_accelerate()
                _sleep(throttle_lift, stop)
                backend.hold_accelerate()
                last_lift = time.time()

            maybe_shift_up()

            _sleep(loop_poll, stop)
    except KeyboardInterrupt:
        log("Stop requested.")
    except screen.FailSafeException:
        log("Failsafe triggered (mouse in corner).")
    finally:
        # telem is the shared process listener — leave it running for the UI.
        backend.close()
        status("stopped")
    return cycles


def main() -> int:
    cfg = load_config()
    max_cycles = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    run(cfg, max_cycles=max_cycles)
    return 0


if __name__ == "__main__":
    sys.exit(main())
