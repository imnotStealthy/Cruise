# Changelog

All notable changes to Cruise are documented here.
This project adheres to [Semantic Versioning](https://semver.org/).

## [1.2.3] - 2026-06-09

### Added
- **Vision resolver debug** — optional confidence logs and near-miss frame capture
  for tuning visual state detection.
- **Local API CSRF protection** — per-process session token required on every
  POST request.

### Fixed
- FAST result and restart confirmation detection now uses one frame capture per
  poll instead of many `GetPixel` calls, cutting visual re-detection latency.
- Restart confirmation timing logs now expose result-to-confirm and
  confirm-to-prerace delays.
- Telemetry sanity checks reject malformed local UDP packets and debounce
  telemetry-only post-race skips.
- Launching a second Cruise desktop instance now attaches a native window to the
  existing server without stopping the owner process.
- Vision debug captures are confined to safe folders and pruned to a bounded
  retention set.

## [1.2.2] - 2026-06-08

### Added
- **Single-instance guard** — launching Cruise again now redirects to the existing
  local UI instead of starting a second bot process.
- **Telemetry log visibility** — the controller view can hide/show the telemetry
  log, and the Control card can scroll in smaller windows.

### Fixed
- FAST result handling now starts from visual results detection instead of waiting
  for delayed `race_off` telemetry.
- Restart confirmation uses bounded fast retries and stops as soon as the visual
  state changes.
- Menu-like visual detections are ignored while telemetry says the race is active,
  preventing accidental menu `Enter` inputs during a run.

## [1.2.1] - 2026-06-06

### Added
- **Automation presets** — `SLOWED` keeps deliberate menu timing; `FAST` uses short
  polling, fast Start Race Event selection, result-screen `X` spam, and restart
  confirmation `Enter` spam.
- **Manual shift assist** — with telemetry enabled, Cruise taps the configured
  upshift key near the rev limiter for manual gearbox setups.
- **EventLab code** — added `362 177 064` for `15 Seconds = 10 Skill Points`.

### Fixed
- Start Race Event selection now detects the highlighted pre-race row and
  normalizes from Difficulty & Settings, Starting Grid, or Quit Race before
  pressing Enter.
- Restart confirmation now forces `Yes`; if `No` is highlighted it moves up before
  confirming.
- Start/Stop UI requests are guarded to avoid accidental double-submit.

### Changed
- Runtime `config.json` and `cars.json` now live in `%USERPROFILE%\.cruise`;
  builds no longer copy config beside `dist\Cruise.exe`.

## [1.2.0] - 2026-06-04

### Added
- **Forza "Data Out" telemetry (UDP)** — reads real speed, gear and car from the
  game's telemetry stream. New **TELEMETRY** section with enable toggle, host
  (`127.0.0.1` / `localhost`) and port, plus a live readout. Config:
  `telemetry_enabled`, `telemetry_host`, `telemetry_port`.
- **Rewind-based stuck recovery, speed-gated** — when the car is genuinely stopped
  (telemetry speed ≈ 0) the bot taps the in-game **Rewind** (keyboard `R` / pad `Y`)
  to snap it back onto the track. Because it gates on real speed, a jump or an
  off-road excursion (still moving fast) no longer triggers a false rewind. Falls
  back to the visual motion check when telemetry is off. Config: `recover_mode`
  (`rewind` | `maneuver`), `rewind_key`, `rewind_wait_s`, `stuck_speed_kmh`.
- **Discord Rich Presence** — shows your live FH6 car name, `speed km/h - Gear N`
  and a session timer on your Discord profile, with a `forza` art asset. Dedicated
  **DISCORD RICH PRESENCE** section to show/hide it. The timer follows the **FH6
  game session** (process start time), not Cruise. Bundled car-name database
  (`cars.json`, ordinal → name) with a local cache and remote refresh. Updates at
  Discord's rate-limit floor (one every 4 s). Config: `rich_presence_enabled`,
  `rich_presence_interval_s`, `discord_client_id`.
- **Menu-aware presence** — shows *In the menus* when `IsRaceOn` is 0 (paused or in
  a menu) instead of a misleading `0 km/h - Gear 0`.
- **First-run self-generation** — `config.json` and `cars.json` are bundled inside
  the exe and written next to it on first launch, so a standalone `Cruise.exe` works
  with no extra files.
- **Robust results detection (lime-band scan)** — the results screen is now matched
  by counting lime pixels across a horizontal band instead of three fixed points,
  so overlaid text/numbers or a shifted layout no longer break the restart.
- **Multi-attempt pause-menu resume** — the resume key is retried up to
  `menu_resume_tries` times (default 3) and the counter resets on every focus loss,
  so returning from an alt-tab reliably dismisses the pause menu.

### Changed
- The **LONG DISTANCE** mode tab is replaced by the **TELEMETRY** and **DISCORD
  RICH PRESENCE** top-level sections. The corner aids (Throttle Modulation / Launch
  Ease-in) are now always available under **ADVANCED**.
- Stuck recovery defaults to **Rewind** (`recover_mode: "rewind"`); the previous
  reverse-and-steer maneuver is still available via `recover_mode: "maneuver"`.
- The desktop (WebView2) build runs with GPU / DirectComposition disabled — Cruise
  needs no GPU, stays lighter, and the window is capturable by OBS' Window Capture
  (use the "Windows 10 (1903 and up)" capture method for Chromium-based windows).

### Fixed
- **DPI awareness** — the process is now per-monitor-DPI-aware. On displays scaled
  above 100% the bot mis-sampled pixels in **windowed** mode (it worked fullscreen
  because sampling is proportional from the origin), so the results screen was never
  detected and the race never restarted. Detection now lands on the right pixels in
  windowed mode.

## [1.1.0] - 2026-05-23

### Added
- **Elapsed time display** (HH:MM:SS) in the HUD — shows how long the bot has been
  running since START; resets on STOP. Seeded from the server on reconnect so the
  counter is accurate even after a page refresh.
- **Mode split UI** — top-level SKILL POINTS / LONG DISTANCE toggle above the
  existing sub-tabs. Each mode has its own EventLab code library AND its own Setup
  Guide (Long Distance uses the 2019 Toyota Tacoma TRD Pro Forza Edition). Controller
  is shared.
- **Ultra Fast Colossus!!** EventLab code (`100 489 171`) in the Long Distance library:
  DEX810, 6 laps, 226.3 km, High Speed / Asphalt, Anything Goes.
- **Collision / stuck recovery** — samples the scene each poll while racing; if the
  car stops moving (hit a vehicle or wall) it backs up and steers to clear it,
  alternating side each time. Tunable in config.json (`recovery_enabled`,
  `stuck_after_s`, `recover_reverse_s`, `recover_steer_s`, `reverse_key`,
  `steer_left_key`, `steer_right_key`).
- **Manual Pause / Resume** button next to START — releases inputs and idles the loop
  without stopping it; `/api/pause` and `/api/resume` endpoints.
- **Throttle modulation** (opt-in checkbox / `throttle_modulation`) — briefly lifts the
  throttle on a cycle (`throttle_hold_s` / `throttle_lift_s`) so the in-game braking
  assist can scrub speed into corners. The bot otherwise holds full throttle with no
  speed/obstacle awareness; leave off for hold-throttle AFK tracks.
- **Launch ease-in** (opt-in checkbox / `launch_ease`, `launch_ease_s`) — feathers the
  throttle for the first seconds of each start/restart so the car doesn't wheelspin and
  veer at GO. Useful when Traction Control is OFF (e.g. running assists-off for max CR).
- **Per-mode Setup Guide notes** explaining the in-game CR-vs-stability trade-off:
  Auto-Steering + Automatic shifting are mandatory; Skill Points (short tracks) can
  run assists-off for a higher difficulty bonus, Long Distance should keep Traction
  &amp; Stability ON for reliability over long unattended runs.
- **Advanced (corner aids) section** — Throttle Modulation and Launch Ease-in are now
  grouped under a collapsible ADVANCED toggle shown **only in Long Distance mode**
  (irrelevant on hold-W Skill Points tracks).
- **Throttle re-assert** — the accelerate key is re-pressed every poll while racing, so
  an accidental manual key press/release (bumping W) can no longer desync the hold and
  silently stop the car.

### Changed
- SSE status events now include an `elapsed_s` field (integer seconds since start,
  0 when stopped). Used by the frontend to seed the elapsed timer on connect.
- Controller config fields use a two-column layout (collapsing to one on very narrow
  windows) so the START/PAUSE buttons stay visible at any aspect ratio, including 21:9.
- Long Distance Setup Guide now recommends **Drivatar Difficulty: Average** — aggressive
  (Unbeatable) AI keeps catching and ramming a car that must brake for corners; Average
  AI stays calm and you pull away, still +50% CR.

### Fixed
- **Pause-menu detection recalibrated** — the guard now samples three stable teal
  background points instead of one dark point and a spot on the changing World Map
  preview, so the bot reliably presses Esc to resume the race after an alt-tab.

## [1.0.0] - 2026-05-23

First public release.

### Added
- AFK auto-farm loop for Forza Horizon 6 EventLab races: holds acceleration and
  loops the race automatically.
- Screen-state detection by pixel color (pre-race menu, results, pause menu),
  relative to the FH6 window — generic across EventLab events, no per-race calibration.
- Input backends: keyboard (DirectInput) and emulated Xbox 360 controller (vgamepad / ViGEm).
- "Race telemetry" web UI with three tabs: Controller, Setup Guide, EventLab Codes.
- EventLab code library with copy-to-clipboard.
- Guards: auto-pause when FH6 loses focus (alt-tab) or a pause/dashboard menu appears,
  with a single Esc/B auto-dismiss to resume.
- Launch-at-GO: holds throttle through loading and the 3-2-1 countdown.
- Instant start: auto-focuses the FH6 window before accelerating.
- Lap counter (one lap per "Start Race Event").
- Window-adaptive detection: fullscreen, windowed, any size/position, multi-monitor.
- Mouse-corner failsafe (top-left) for an emergency stop.
- Single-instance lock (named Windows mutex): launching Cruise again surfaces the
  existing instance instead of opening a second one.

### Performance & size
- Single-file build (~18 MB).
- Screen reading via raw Windows GDI (`screen.py`) instead of Pillow.
- UI served by the stdlib `http.server` instead of FastAPI/uvicorn (no pydantic).
- One screen Device Context reused per poll instead of one per pixel read.
- Cached FH6 window handle with cheap revalidation instead of a full window scan each poll.

### Security
- Local server binds `127.0.0.1` only.
- `Host` and `Origin` header validation (anti DNS-rebinding) on the API.
- Path-traversal guard on static file serving.
