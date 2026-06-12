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


def pixel_score(p: dict, rect: tuple[int, int, int, int]) -> tuple[bool, float]:
    """(passed, confidence). Confidence is 1.0 at the exact colour and decays to
    0.0 at 2x tolerance -> near-misses are visible in debug logs instead of an
    opaque boolean."""
    ox, oy, w, h = rect
    got = screen.pixel(ox + resolve(p["x"], w), oy + resolve(p["y"], h))
    tol = max(1, int(p.get("tol", 20)))
    rgb = p["rgb"]
    delta = max(abs(got[k] - rgb[k]) for k in range(3))
    return delta <= tol, max(0.0, 1.0 - delta / (2.0 * tol))


def pixel_matches(p: dict, rect: tuple[int, int, int, int]) -> bool:
    return pixel_score(p, rect)[0]


def band_score(band: dict, rect: tuple[int, int, int, int]) -> tuple[bool, float]:
    """(passed, confidence) for a horizontal colour band at band["y"]: passed when
    >= min_hits sampled pixels across [x0, x1] match the target colour. Robust to
    text/numbers/edges overlaid on the bar and to layout shifts (counts the
    colour, not exact points) — unlike a few fixed points where one landing on
    text (e.g. the FH6 results lime header) breaks an all-match. Confidence is
    hits/min_hits capped at 1.0."""
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
    need = max(1, int(band.get("min_hits", 20)))
    return hits >= need, min(1.0, hits / need)


def band_matches(band: dict, rect: tuple[int, int, int, int]) -> bool:
    return band_score(band, rect)[0]


def state_score(state: dict, rect: tuple[int, int, int, int]) -> tuple[bool, float, str]:
    """(passed, confidence, detail) for one state. match_mode "all": every check
    must pass, confidence/detail come from the weakest check (the one that breaks
    first). "any": the strongest check decides."""
    checks: list[tuple[bool, float, str]] = []
    for p in state.get("pixels", []):
        ok, conf = pixel_score(p, rect)
        checks.append((ok, conf, f"pixel({p['x']},{p['y']})"))
    if "band" in state:
        ok, conf = band_score(state["band"], rect)
        checks.append((ok, conf, f"band(y={state['band']['y']})"))
    if not checks:
        return False, 0.0, "no checks"
    if state.get("match_mode", "all") == "all":
        passed = all(c[0] for c in checks)
        weakest = min(checks, key=lambda c: c[1])
        return passed, weakest[1], weakest[2]
    best = max(checks, key=lambda c: c[1])
    return best[0], best[1], best[2]


def state_active(state: dict, rect: tuple[int, int, int, int]) -> bool:
    return state_score(state, rect)[0]


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


def detect_state(cfg: dict, rect: tuple[int, int, int, int] | None = None, debug=None) -> dict | None:
    """Deterministic resolver: score every state, keep the passing ones, pick the
    winner by (priority desc, confidence desc) — overlapping states (the restart
    popup over the results screen) resolve by explicit priority, equal priorities
    by confidence. `debug` (VisionDebug) logs scores/decisions and saves frames."""
    if rect is None:
        rect = detection_rect(cfg)
    scored: list[tuple[dict, bool, float, str]] = []
    with screen.frame_session(rect):  # one BitBlt for all samples in the poll
        for state in cfg["states"]:
            passed, conf, detail = state_score(state, rect)
            scored.append((state, passed, conf, detail))
    active = [c for c in scored if c[1]]
    winner = max(active, key=lambda c: (int(c[0].get("priority", 0)), c[2]))[0] if active else None
    if winner is not None:
        winner["_conf"] = next(c[2] for c in scored if c[0] is winner)
    if debug is not None:
        debug.observe(scored, winner, rect)
    return winner


class VisionDebug:
    """Opt-in visual debug (config "vision_debug": true): logs the resolver
    decision with its confidence on every change, and on near-misses (best
    candidate scored >= NEAR but did not pass) logs the rejection reason and
    saves the capture to vision_debug_dir (default tools/debug_frames).
    Throttled (one frame per SAVE_EVERY_S, MAX_FRAMES total) so the 100 Hz FAST
    poll cannot flood the log or the disk."""

    NEAR = 0.5
    SAVE_EVERY_S = 2.0
    MAX_FRAMES = 200       # per run
    KEEP_ON_DISK = 300     # retention: oldest PNGs beyond this are pruned

    def __init__(self, cfg: dict, log) -> None:
        self.enabled = bool(cfg.get("vision_debug", False))
        self.log = log
        self.dir = self._safe_dir(str(cfg.get("vision_debug_dir", "tools/debug_frames")))
        self._last_name: str | None = "__boot__"
        self._last_save = 0.0
        self._saved = 0

    @staticmethod
    def _safe_dir(raw: str) -> Path:
        """Confine the debug dir to the app dir or ~/.cruise: screenshots contain
        the whole screen (overlays, notifications), so a config value must not be
        able to scatter them anywhere on disk."""
        path = Path(raw)
        try:
            target = (path if path.is_absolute() else _BASE / path).resolve()
            for root in (_BASE.resolve(), DATA_DIR.resolve()):
                if target == root or root in target.parents:
                    return target
        except OSError:
            pass
        return DATA_DIR / "debug_frames"

    def _prune(self) -> None:
        try:
            frames = sorted(self.dir.glob("*.png"), key=lambda p: p.stat().st_mtime)
            for old in frames[:-self.KEEP_ON_DISK]:
                old.unlink()
        except OSError:
            pass

    def observe(self, scored, winner, rect) -> None:
        if not self.enabled:
            return
        name = winner.get("name") if winner else None
        if name != self._last_name:
            self._last_name = name
            if winner is not None:
                _, _, conf, detail = next(c for c in scored if c[0] is winner)
                self.log(f"[vision] state={name} conf={conf:.2f} via {detail}")
        if winner is not None:
            return
        near = [c for c in scored if not c[1] and c[2] >= self.NEAR]
        if not near:
            return
        best = max(near, key=lambda c: c[2])
        now = time.time()
        if now - self._last_save < self.SAVE_EVERY_S or self._saved >= self.MAX_FRAMES:
            return
        self.log(
            f"[vision] state=none; near-miss {best[0].get('name')} "
            f"conf={best[2]:.2f} rejected by {best[3]}"
        )
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path = self.dir / f"{time.strftime('%Y%m%d_%H%M%S')}_{best[0].get('name')}.png"
            screen.save_png(str(path), rect)
            self._prune()
            self._last_save = now
            self._saved += 1
            self.log(f"[vision] frame saved -> {path}")
        except OSError as e:
            self.log(f"[vision] frame save failed: {e}")


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


def _count(value, default: int, minimum: int = 0, maximum: int = 100) -> int:
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = default
    return max(minimum, min(maximum, count))


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
    loop_poll = _duration(cfg.get("loop_poll_s"), 1.0, 0.01)
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
    # A guard menu (settings_menu) seen on a single poll can be a lime overlay /
    # transition frame: without fresh telemetry, require the same guard state on
    # N consecutive polls before sending esc.
    guard_confirm_polls = _count(
        cfg.get("pause_menu_confirm_polls", cfg.get("settings_confirm_polls")), 2, 1, 10
    )
    # In the fast preset loop_poll is 10ms, so N polls alone is no time filter:
    # the guard state must also persist for guard_confirm_s of wall time.
    guard_confirm_s = _duration(cfg.get("settings_confirm_s"), 0.5, 0.0)
    guard_streak_name = None
    guard_streak = 0
    guard_streak_since = 0.0
    # After menu_resume_tries failed esc taps, retry instead of idling forever
    # (a real pause menu we opened ourselves must always be escapable).
    menu_retry_s = _duration(cfg.get("menu_resume_retry_s"), 6.0, 1.0)
    last_menu_tap = 0.0
    # Pause-menu resume pacing: with FH6 focused the whole time, a confirmed
    # pause menu must stay stable for pause_resume_delay before the first esc.
    # Right after an alt-tab back, resume fast (pause_resume_after_focus).
    pause_resume_delay = _duration(cfg.get("pause_menu_resume_delay_s"), 5.0, 0.0)
    pause_resume_after_focus = _duration(cfg.get("pause_menu_resume_after_focus_s"), 0.25, 0.0)
    pause_resume_retry = _duration(cfg.get("pause_menu_resume_retry_s"), 1.0, 0.1)
    focus_lost_at = 0.0
    focus_returned_at = 0.0
    pause_wait_logged = False
    # Lap latch: prerace_menu Enter only arms lap_pending; cycles increments once
    # the launch is confirmed (telemetry race_on, or visually stable race after
    # lap_confirm_s without telemetry) -> repeated prerace polls / cursor fixes
    # can never double-count.
    lap_pending = False
    lap_pending_at = 0.0
    lap_confirm_s = _duration(cfg.get("lap_confirm_s"), 5.0, 0.0)

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
    race_seen = False
    post_race_skip_sent = False
    race_off_since = None
    # One spoofed/glitched race_on=false packet must not fire the blind post-race
    # x-spam mid-race: require race_off to hold for this long first. The visual
    # results path stays primary and is unaffected.
    race_off_confirm = _duration(cfg.get("race_off_confirm_s"), 0.3, 0.0)
    await_confirm_until = 0.0
    relaunch_drive_until = 0.0
    ignored_racing_state = None
    race_on_confirm = None
    visual_results_at = None
    visual_results_delay_logged = False
    first_x_at = None          # first results 'x' -> [timing] x_to_restart_confirm
    confirm_detected_at = None  # popup seen -> [timing] restart_confirm_* logs

    vision_dbg = VisionDebug(cfg, log)
    if vision_dbg.enabled:
        log(f"Vision debug: ON -> {vision_dbg.dir}")
        log("Vision debug captures gameplay screenshots; disable before release.")

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

    def state_changed_from(name: str, rect: tuple[int, int, int, int]) -> bool:
        current = detect_state(cfg, rect)
        return current is None or current.get("name") != name

    def wait_until_state_change(name: str, rect: tuple[int, int, int, int], timeout_s: float) -> bool:
        end = time.time() + max(0.0, timeout_s)
        while time.time() < end:
            if stop is not None and stop.is_set():
                return False
            if state_changed_from(name, rect):
                return True
            _sleep(min(loop_poll, 0.05, end - time.time()), stop)
        return False

    def wait_until_launch_signal(name: str, rect: tuple[int, int, int, int], timeout_s: float) -> bool:
        end = time.time() + max(0.0, timeout_s)
        while time.time() < end:
            if stop is not None and stop.is_set():
                return False
            fresh, race_on, _speed_kmh = telem.snapshot() if telem else (False, False, 0.0)
            if fresh and race_on:
                return True
            if state_changed_from(name, rect):
                return True
            _sleep(min(loop_poll, 0.05, end - time.time()), stop)
        return False

    def action_hold_s(action: dict):
        if backend.name == "gamepad" and action.get("gamepad_tap_hold_s") is not None:
            return action.get("gamepad_tap_hold_s")
        return action.get("tap_hold_s")

    def tap_step(step: dict) -> None:
        backend.tap(step["key"], action_hold_s(step))

    def execute_steps(steps: list[dict]) -> list[str]:
        keys = []
        for step in steps:
            keys.append(step["key"])
            tap_step(step)
            _sleep(_duration(step.get("wait"), 0.5), stop)
        return keys

    def execute_spam(spam: dict | None, state_name: str, rect: tuple[int, int, int, int]) -> tuple[list[str], bool]:
        if not spam:
            return [], False
        key = spam.get("key")
        if not key:
            return [], False
        count = _count(spam.get("count"), 1, 1)
        interval = _duration(spam.get("interval_s"), 0.05)
        hold_s = action_hold_s(spam)
        duration_s = spam.get("duration_s")
        stop_on_change = bool(spam.get("stop_on_state_change", False))
        blind = bool(spam.get("blind", False))
        check_every = _count(spam.get("check_state_every"), 1, 1)
        end = time.time() + _duration(duration_s, 0.0) if duration_s is not None else None
        keys = []
        changed = False
        sent = 0
        while sent < count and (end is None or time.time() < end):
            if stop is not None and stop.is_set():
                break
            backend.tap(key, hold_s)
            keys.append(key)
            sent += 1
            if not blind and stop_on_change and sent % check_every == 0 and state_changed_from(state_name, rect):
                changed = True
                break
            _sleep(interval, stop)
            if not blind and stop_on_change and sent % check_every == 0 and state_changed_from(state_name, rect):
                changed = True
                break
        if not blind and not changed and stop_on_change and state_changed_from(state_name, rect):
            changed = True
        if not changed and spam.get("fallback_keys"):
            log(f"[{state_name}] fast spam did not change state; fallback keys.")
            keys.extend(execute_steps(spam.get("fallback_keys", [])))
            changed = state_changed_from(state_name, rect)
        then_spam = spam.get("then_spam", [])
        if isinstance(then_spam, dict):
            then_spam = [then_spam]
        for followup in then_spam:
            follow_keys, follow_changed = execute_spam(followup, state_name, rect)
            keys.extend(follow_keys)
            changed = changed or follow_changed
        return keys, changed

    def execute_results_combo(spam: dict | None, rect: tuple[int, int, int, int]) -> tuple[list[str], bool]:
        if not spam:
            return [], False
        timeout_s = _duration(spam.get("duration_s"), 3.0, 0.1)
        interval = _duration(spam.get("interval_s"), 0.03)
        hold_s = action_hold_s(spam)
        end = time.time() + timeout_s
        keys = []
        changed = False
        def left_results() -> bool:
            # Any state other than "results" (incl. restart_confirm, prerace_menu,
            # none) ends the combo: the popup is handed to its own handler so the
            # selected-row normalization picks Yes (a blind Enter here could hit No).
            current = detect_state(cfg, rect)
            return current is None or current.get("name") != "results"

        while time.time() < end:
            if stop is not None and stop.is_set():
                break
            backend.tap("x", hold_s)
            keys.append("x")
            _sleep(interval, stop)
            if left_results():
                changed = True
                break
            backend.tap("enter", hold_s)
            keys.append("enter")
            _sleep(interval, stop)
            if left_results():
                changed = True
                break
        if not changed and spam.get("fallback_keys"):
            log("[results] fast visual combo did not change state; fallback keys.")
            keys.extend(execute_steps(spam.get("fallback_keys", [])))
            changed = state_changed_from("results", rect)
        return keys, changed

    def maybe_fast_post_race_skip(rect: tuple[int, int, int, int]) -> bool:
        nonlocal race_seen, post_race_skip_sent, race_off_since
        if not (cfg.get("automation_preset") == "fast" and cfg.get("fast_post_race_skip", True) and telem):
            return False
        fresh, race_on, _speed_kmh = telem.snapshot()
        if not fresh:
            return False
        if race_on:
            race_seen = True
            post_race_skip_sent = False
            race_off_since = None
            return False
        if not race_seen or post_race_skip_sent:
            return False
        now = time.time()
        if race_off_since is None:
            race_off_since = now
            return False
        if now - race_off_since < race_off_confirm:
            return False
        backend.release_accelerate()
        backend.release_steer()
        spam = cfg.get("fast_post_race_spam", {
            "key": "x",
            "count": 10,
            "interval_s": 0.003,
            "tap_hold_s": 0.01,
            "gamepad_tap_hold_s": 0.015,
            "stop_on_state_change": True,
            "check_state_every": 4,
        })
        log("[post_race] telemetry race_off -> fast result skip.")
        execute_spam(spam, "post_race", rect)
        post_race_skip_sent = True
        return True

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
                if focus_lost_at == 0.0:
                    focus_lost_at = time.time()
                    log("[focus] FH6 lost; waiting")
                    log("[pause_menu] skipped: FH6 not foreground")
                enter_pause("Forza Horizon 6 not focused (alt-tab)")
                menu_resumes = 0  # fresh resume attempts each time focus returns
                # Stale streak from before the alt-tab must not skip the
                # confirmation polls once focus returns.
                guard_streak_name = None
                guard_streak = 0
                pause_wait_logged = False
                _sleep(loop_poll, stop)
                continue
            if focus_lost_at:
                focus_returned_at = time.time()
                focus_lost_at = 0.0
                log("[focus] FH6 active again")

            rect = win[3] if win else detection_rect(cfg)
            state = detect_state(cfg, rect, vision_dbg)
            fresh_state, race_on_state, _ = telem.snapshot() if telem else (False, False, 0.0)
            if state is not None and state.get("name") == "results" and visual_results_at is None:
                visual_results_at = time.time()
                visual_results_delay_logged = False
                if fresh_state and race_on_state:
                    log("[results] visual detected while telemetry race_on=true.")
            if visual_results_at is not None and fresh_state and not race_on_state and not visual_results_delay_logged:
                log(f"[timing] visual_results_to_race_off: {time.time() - visual_results_at:.2f}s")
                visual_results_delay_logged = True
            if fresh_state and race_on_state and state is not None:
                name = state.get("name")
                if name in ("prerace_menu", "settings_menu", "menu"):
                    if ignored_racing_state != name:
                        log(f"[guard] {name} rejected because race active (telemetry race_on=true).")
                        ignored_racing_state = name
                    state = None
                else:
                    ignored_racing_state = None
                    # While race_on=true, results/restart_confirm must be seen on
                    # 2 consecutive polls before any menu key is sent -> one stray
                    # frame matching the lime band can't fire x/enter mid-race.
                    if name in ("results", "restart_confirm") and race_on_confirm != name:
                        race_on_confirm = name
                        state = None
            elif not (fresh_state and race_on_state):
                ignored_racing_state = None
                race_on_confirm = None

            # Guard 2: "guard" screen (pause menu / dashboard).
            if state is not None and state.get("guard", False):
                now = time.time()
                if cfg.get("automation_preset") == "fast" and state.get("name") == "settings_menu":
                    if now < await_confirm_until:
                        _sleep(loop_poll, stop)
                        continue
                    if now < relaunch_drive_until:
                        state = None
                    else:
                        await_confirm_until = 0.0
                        relaunch_drive_until = 0.0
                # Track consecutive polls of the same guard state (transition/
                # overlay frames break the streak by resolving to another state).
                if state is not None and state.get("name") == guard_streak_name:
                    guard_streak += 1
                elif state is not None:
                    guard_streak_name = state.get("name")
                    guard_streak = 1
                    guard_streak_since = now
                # Harden EVERY guard state that can send a resume key (menu and
                # settings_menu): a single lime/teal frame mid-race must never
                # fire esc and open the real pause menu.
                if state is not None and state.get("resume_key"):
                    gname = state.get("name")
                    conf = state.get("_conf", 0.0)
                    fresh_g, race_on_g, speed_g = telem.snapshot() if telem else (False, False, 0.0)
                    if guard_streak == 1:  # once per streak, not per 10ms poll
                        log(
                            f"[pause_menu] candidate count={guard_streak} {gname} conf={conf:.2f} "
                            f"race_on={race_on_g if fresh_g else 'n/a'} speed={speed_g:.0f}"
                        )
                    if fresh_g and race_on_g:
                        # Belt-and-braces: the race_on filter above already nulls
                        # this, but never esc while telemetry says racing.
                        log(f"[pause_menu] resume skipped: telemetry race_on=true ({gname})")
                        state = None
                    elif fresh_g and speed_g > stuck_speed:
                        log(f"[guard] {gname} rejected: moving at {speed_g:.0f} km/h")
                        state = None
                    elif conf < 0.5:
                        # Pixel confidence is 1-delta/(2*tol): a passing pixel
                        # scores 0.5..1.0, so requiring 1.0 rejected every real
                        # pause menu. 0.5 = "all checks within tolerance".
                        log(f"[guard] {gname} rejected: low confidence {conf:.2f}")
                        state = None
                    elif guard_streak < guard_confirm_polls or now - guard_streak_since < guard_confirm_s:
                        if guard_streak == 1:
                            log(f"[guard] {gname} unconfirmed; need {guard_confirm_polls} polls over {guard_confirm_s:.2f}s.")
                        _sleep(loop_poll, stop)
                        continue
                if state is None:
                    guard_streak_name = None
                    guard_streak = 0
                    pause_wait_logged = False
                else:
                    if lap_pending and state.get("name") == "menu":
                        log("[lap] pending launch cancelled (back to main menu).")
                        lap_pending = False
                    rk = state.get("resume_key")
                    # Auto-resume: tap the resume key (esc/B) to close the menu and
                    # resume the race, retrying up to menu_resume_tries (the first tap
                    # often misses right after an alt-tab while focus is still settling).
                    # Re-detection each loop stops as soon as the menu is gone; the
                    # cap bounds damage if it's actually a stuck dashboard.
                    if rk and menu_resumes >= menu_resume_tries and now - last_menu_tap >= menu_retry_s:
                        # Menu still confirmed long after giving up -> it is real
                        # (possibly opened by our own stray esc); retry escaping
                        # instead of idling in pause forever.
                        log(f"[pause_menu] '{state['name']}' still present after give-up; retrying resume.")
                        menu_resumes = 0
                    if rk and menu_resumes == 0:
                        # Before the first esc of a streak: the confirmed menu must
                        # stay stable for resume_wait. Short wait right after an
                        # alt-tab back, longer when FH6 was focused the whole time.
                        resume_wait = pause_resume_delay
                        if focus_returned_at and now - focus_returned_at < pause_resume_delay:
                            resume_wait = pause_resume_after_focus
                        if now - guard_streak_since < resume_wait:
                            if not pause_wait_logged:
                                log(f"[pause_menu] confirmed; wait={resume_wait:.2f}s")
                                pause_wait_logged = True
                            status("paused")
                            _sleep(loop_poll, stop)
                            continue
                    if rk and menu_resumes < menu_resume_tries:
                        if menu_resumes > 0:
                            log("[pause_menu] still visible after resume")
                        log(
                            f"[pause_menu] resume -> {rk} "
                            f"(attempt {menu_resumes + 1}/{menu_resume_tries})"
                        )
                        backend.tap(rk)
                        last_menu_tap = time.time()
                        menu_resumes += 1
                        pause_wait_logged = False
                        status("paused")
                        _sleep(pause_resume_retry, stop)
                        continue
                    enter_pause(f"'{state['name']}' menu")
                    _sleep(loop_poll, stop)
                    continue

            if paused:
                log("Resumed.")
                paused = False
            menu_resumes = 0
            # No guard state this poll -> any settings_menu streak is broken.
            if state is None or not state.get("guard", False):
                guard_streak_name = None
                guard_streak = 0
                pause_wait_logged = False

            if state is not None:
                reset_stuck()  # menus/results/countdown are not "racing"
                backend.release_accelerate()
                backend.release_steer()
                handler_t0 = time.time()
                if state["name"] == "results" and visual_results_at is not None and first_x_at is None:
                    log(f"[timing] results_detected_to_x: {handler_t0 - visual_results_at:.2f}s")
                    first_x_at = handler_t0  # 'x' goes out right below (steps/combo)
                elif state["name"] == "restart_confirm":
                    if first_x_at is not None:
                        log(f"[timing] x_to_restart_confirm: {handler_t0 - first_x_at:.2f}s")
                        first_x_at = None
                    if confirm_detected_at is None:
                        confirm_detected_at = handler_t0
                elif state["name"] == "prerace_menu" and confirm_detected_at is not None:
                    log(f"[timing] restart_confirm_to_prerace: {handler_t0 - confirm_detected_at:.2f}s")
                    confirm_detected_at = None
                menu_keys = selected_menu_keys(state, rect)
                if menu_keys is None:
                    fallback = state.get("selected_menu_fallback_keys")
                    if fallback is None:
                        log(f"[{state['name']}] selected menu row not detected; waiting.")
                        _sleep(loop_poll, stop)
                        continue
                    log(f"[{state['name']}] selected menu row not detected; fallback keys.")
                    menu_keys = fallback
                steps = menu_keys + state.get("keys", [])
                keys = [k["key"] for k in steps]
                spam = state.get("spam")
                if spam:
                    keys.append(f"{spam.get('key', '?')}*{spam.get('count', 1)}")
                log(f"[{state['name']}] -> {keys}")
                status(state["name"])
                execute_steps(steps)
                if state["name"] == "restart_confirm" and confirm_detected_at is not None and state.get("spam"):
                    # fast preset: the first Enter is the next tap of the spam below
                    log(f"[timing] restart_confirm_detected_to_enter: {time.time() - confirm_detected_at:.2f}s")
                if cfg.get("automation_preset") == "fast" and state["name"] == "results":
                    _, spam_changed = execute_results_combo(spam, rect)
                    post_race_skip_sent = True
                else:
                    _, spam_changed = execute_spam(spam, state["name"], rect)
                if cfg.get("automation_preset") == "fast" and spam_changed:
                    now = time.time()
                    if state["name"] == "results":
                        await_confirm_until = now + _duration(cfg.get("await_confirm_s"), 3.0, 0.0)
                    elif state["name"] == "restart_confirm":
                        await_confirm_until = 0.0
                        relaunch_drive_until = now + _duration(cfg.get("relaunch_drive_s"), 8.0, 0.0)
                        backend.hold_accelerate()
                # Lap latch: the count_lap state (Start Race Event) only ARMS the
                # counter; cycles increments once the launch is confirmed in the
                # racing branch below -> repeated prerace polls, cursor fixes or a
                # return to the menu can never add phantom laps.
                if state.get("count_lap", False):
                    if lap_pending:
                        log("[lap] duplicate prerace ignored (launch already pending).")
                    else:
                        lap_pending = True
                        log(f"[lap] launch pending (Start Race Event sent) cycles={cycles}")
                    lap_pending_at = time.time()
                    await_confirm_until = 0.0
                    relaunch_drive_until = 0.0
                    status(state["name"])
                # Hold the accelerator during loading + 3-2-1-GO countdown
                # -> the car launches right at GO (instead of staying still).
                post_wait = _duration(state.get("post_wait_s", cfg.get("post_restart_wait_s")), 8.0)
                if spam_changed:
                    reset_stuck()
                    continue
                wait_until_change = bool(state.get("wait_until_state_change", False))
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
                        if wait_until_change:
                            if cfg.get("automation_preset") == "fast":
                                wait_until_launch_signal(state["name"], rect, post_wait)
                            else:
                                wait_until_state_change(state["name"], rect, post_wait)
                        else:
                            drive_wait(post_wait)
                else:
                    if wait_until_change:
                        wait_until_state_change(state["name"], rect, post_wait)
                    else:
                        _sleep(post_wait, stop)
                reset_stuck()  # fresh launch -> reset stuck/modulation timers
                continue

            backend.reassert_accelerate()  # re-press each poll -> stray W bump can't desync
            if steer:
                backend.hold_steer()
            status("racing")
            fresh_race, race_on_now, _speed_now = telem.snapshot() if telem else (False, False, 0.0)
            if lap_pending:
                # Confirm the armed launch: telemetry race_on, or (no telemetry)
                # a stable visual race (no state) lap_confirm_s after the Enter.
                reason = None
                if fresh_race and race_on_now:
                    reason = "telemetry_race_on"
                elif not fresh_race and time.time() - lap_pending_at >= lap_confirm_s:
                    reason = "visual_stable"
                if reason:
                    lap_pending = False
                    cycles += 1
                    log(f"[lap] increment reason={reason} cycles={cycles} state=racing")
                    status("racing")
                    if max_cycles and cycles >= max_cycles:
                        log(f"Max laps reached ({cycles}). Clean stop.")
                        break
            if fresh_race and race_on_now:
                race_seen = True
                post_race_skip_sent = False
                await_confirm_until = 0.0
                relaunch_drive_until = 0.0
                race_on_confirm = None
                visual_results_at = None
                visual_results_delay_logged = False
                first_x_at = None
                confirm_detected_at = None

            # Stuck/collision detection: the car hit a vehicle/wall -> rewind (or
            # back up and steer). With telemetry we know real speed, so a jump or
            # off-road run (still fast) can't be mistaken for stuck; otherwise we
            # fall back to "the sampled scene stopped changing".
            if recovery:
                now = time.time()
                fresh, race_on, speed_kmh = (fresh_race, race_on_now, _speed_now) if telem else (False, False, 0.0)
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
            if maybe_fast_post_race_skip(rect):
                continue

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
