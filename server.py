"""Local server for Cruise (stdlib http.server, no FastAPI/uvicorn).

Serves the frontend (web/) and exposes a small API + real-time SSE stream on
127.0.0.1 only. Run: py server.py (opens the browser).

stdlib choice: the app only serves a local single-user UI. http.server +
ThreadingHTTPServer is enough and avoids bundling pydantic/starlette/uvicorn
(~15 MB) into the executable.
"""
from __future__ import annotations

import ctypes
import hmac
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

# Frozen --noconsole: stdout/stderr are None -> avoid logging crashes.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import bot as core
import inputs
import telemetry
import discord_presence

HOST, PORT = "127.0.0.1", 8733
_BASE = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
_MEI = Path(getattr(sys, "_MEIPASS", _BASE))
WEB_DIR = (_MEI / "web") if (_MEI / "web").exists() else (_BASE / "web")
ALLOWED_ORIGINS = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}
ALLOWED_HOSTS = {f"{HOST}:{PORT}", f"localhost:{PORT}"}

# Anti-CSRF: random per-process token. The frontend fetches it via /api/session
# (same-origin only: no CORS headers, so an external page cannot read it) and
# must echo it in X-Cruise-Token on every POST. A cross-site form/fetch can
# still SEND a POST to 127.0.0.1 but cannot know the token -> rejected.
# Never persisted (not in config.json) and never logged.
_SESSION_TOKEN = secrets.token_hex(32)

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".ico": "image/x-icon",
    ".png": "image/png",
    ".svg": "image/svg+xml",
    ".json": "application/json",
}


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


_MUTEX_NAME = "Global\\Cruise_FH6_SingleInstance"
_mutex_handle = None


def already_running() -> bool:
    """Single instance: a named Windows mutex held for the process lifetime.
    Returns True if another Cruise instance already owns it."""
    global _mutex_handle
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateMutexW(None, True, _MUTEX_NAME)
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        if handle:
            kernel32.CloseHandle(handle)
        return True
    _mutex_handle = handle
    return False


def _bad_request(msg: str) -> None:
    raise BadRequest(msg)


def _key(value, field: str, *, allow_empty: bool = False) -> str | None:
    if value is None and allow_empty:
        return None
    if not isinstance(value, str):
        _bad_request(f"{field} must be a string")
    key = value.strip().lower()
    if not key and allow_empty:
        return None
    if not key:
        _bad_request(f"{field} cannot be empty")
    if len(key) > 32 or any(ord(ch) < 33 or ord(ch) > 126 for ch in key):
        _bad_request(f"{field} contains invalid characters")
    return key


def _float_range(value, field: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        _bad_request(f"{field} must be a number")
    if not minimum <= number <= maximum:
        _bad_request(f"{field} must be between {minimum} and {maximum}")
    return number


def _int_range(value, field: str, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        _bad_request(f"{field} must be an integer")
    if not minimum <= number <= maximum:
        _bad_request(f"{field} must be between {minimum} and {maximum}")
    return number


def _state(cfg: dict, name: str) -> dict | None:
    return next((s for s in cfg.get("states", []) if s.get("name") == name), None)


def _apply_automation_preset(cfg: dict, preset: str) -> None:
    presets = {
        "slowed": {
            "loop_poll_s": 1.0,
            "keyboard_tap_hold_s": 0.06,
            "gamepad_tap_hold_s": 0.08,
            "menu_wait_s": 0.55,
            "menu_enter_wait_s": 1.0,
            "results_x_wait_s": 1.2,
            "results_post_wait_s": 1.0,
            "confirm_nav_wait_s": 0.5,
            "confirm_enter_wait_s": 1.0,
            "confirm_post_wait_s": 8.0,
            "prerace_post_wait_s": 8.0,
            "wait_until_state_change": False,
            "fast_post_race_skip": False,
        },
        "fast": {
            "loop_poll_s": 0.01,
            "keyboard_tap_hold_s": 0.02,
            "gamepad_tap_hold_s": 0.025,
            "menu_wait_s": 0.01,
            "menu_enter_wait_s": 0.02,
            "results_spam_count": 100,
            "results_x_wait_s": 0.025,
            "results_retry_s": 3.2,
            "results_post_wait_s": 0.0,
            "confirm_nav_wait_s": 0.02,
            "confirm_spam_count": 24,
            "confirm_enter_wait_s": 0.03,
            "confirm_retry_s": 1.3,
            "confirm_post_wait_s": 0.0,
            "prerace_post_wait_s": 4.5,
            "wait_until_state_change": True,
            "fast_post_race_skip": True,
            "menu_resume_tries": 1,
            "await_confirm_s": 3.0,
            "relaunch_drive_s": 4.5,
        },
    }
    values = presets[preset]
    cfg["automation_preset"] = preset
    cfg["loop_poll_s"] = values["loop_poll_s"]
    cfg["keyboard_tap_hold_s"] = values["keyboard_tap_hold_s"]
    cfg["gamepad_tap_hold_s"] = values["gamepad_tap_hold_s"]
    cfg["fast_post_race_skip"] = values["fast_post_race_skip"]
    cfg["menu_resume_tries"] = values.get("menu_resume_tries", 3)
    cfg["await_confirm_s"] = values.get("await_confirm_s", 0.0)
    cfg["relaunch_drive_s"] = values.get("relaunch_drive_s", 0.0)
    if preset == "fast":
        cfg["fast_post_race_spam"] = {
            "key": "x",
            "count": 45,
            "interval_s": 0.035,
            "tap_hold_s": values["keyboard_tap_hold_s"],
            "gamepad_tap_hold_s": values["gamepad_tap_hold_s"],
            "stop_on_state_change": True,
            "check_state_every": 1,
        }
    else:
        cfg.pop("fast_post_race_spam", None)

    prerace = _state(cfg, "prerace_menu")
    if prerace:
        for row in prerace.get("selected_menu", {}).get("rows", []):
            for step in row.get("keys", []):
                step["wait"] = values["menu_wait_s"]
        for step in prerace.get("keys", []):
            step["wait"] = values["menu_enter_wait_s"]
            step["tap_hold_s"] = values["keyboard_tap_hold_s"]
            step["gamepad_tap_hold_s"] = values["gamepad_tap_hold_s"]
        prerace["post_wait_s"] = values["prerace_post_wait_s"]
        prerace["wait_until_state_change"] = values["wait_until_state_change"]

    results = _state(cfg, "results")
    if results:
        if preset == "fast":
            results["keys"] = []
            results["spam"] = {
                "key": "x",
                "count": values["results_spam_count"],
                "interval_s": values["results_x_wait_s"],
                "duration_s": values["results_retry_s"],
                "tap_hold_s": values["keyboard_tap_hold_s"],
                "gamepad_tap_hold_s": values["gamepad_tap_hold_s"],
                "stop_on_state_change": True,
                "check_state_every": 1,
                "fallback_keys": [{"key": "x", "wait": 0.08, "tap_hold_s": 0.04, "gamepad_tap_hold_s": 0.05}],
            }
        else:
            results["keys"] = [{"key": "x", "wait": values["results_x_wait_s"]}]
            results.pop("spam", None)
        results["post_wait_s"] = values["results_post_wait_s"]
        results["wait_until_state_change"] = values["wait_until_state_change"]

    confirm = _state(cfg, "restart_confirm")
    if confirm:
        for row in confirm.get("selected_menu", {}).get("rows", []):
            for step in row.get("keys", []):
                step["wait"] = values["confirm_nav_wait_s"]
                step["tap_hold_s"] = values["keyboard_tap_hold_s"]
                step["gamepad_tap_hold_s"] = values["gamepad_tap_hold_s"]
        if preset == "fast":
            confirm["keys"] = []
            confirm["selected_menu_fallback_keys"] = [{
                "key": "up",
                "wait": values["confirm_nav_wait_s"],
                "tap_hold_s": values["keyboard_tap_hold_s"],
                "gamepad_tap_hold_s": values["gamepad_tap_hold_s"],
            }]
            confirm["spam"] = {
                "key": "enter",
                "count": values["confirm_spam_count"],
                "interval_s": values["confirm_enter_wait_s"],
                "duration_s": values["confirm_retry_s"],
                "tap_hold_s": values["keyboard_tap_hold_s"],
                "gamepad_tap_hold_s": values["gamepad_tap_hold_s"],
                "stop_on_state_change": True,
                "check_state_every": 1,
                "fallback_keys": [{"key": "enter", "wait": 0.08, "tap_hold_s": 0.04, "gamepad_tap_hold_s": 0.05}],
            }
        else:
            confirm["keys"] = [{"key": "enter", "wait": values["confirm_enter_wait_s"]}]
            confirm.pop("spam", None)
            confirm.pop("selected_menu_fallback_keys", None)
        confirm["post_wait_s"] = values["confirm_post_wait_s"]
        confirm["wait_until_state_change"] = values["wait_until_state_change"]


def _apply_config_update(cfg: dict, data: dict) -> dict:
    if "input_backend" in data:
        if data["input_backend"] not in ("keyboard", "gamepad"):
            _bad_request("input_backend must be keyboard or gamepad")
        cfg["input_backend"] = data["input_backend"]
    if "accelerate_key" in data:
        cfg["accelerate_key"] = _key(data["accelerate_key"], "accelerate_key")
    if "steer_key" in data:
        cfg["steer_key"] = _key(data["steer_key"], "steer_key", allow_empty=True)
    if "start_delay_s" in data:
        cfg["start_delay_s"] = _float_range(data["start_delay_s"], "start_delay_s", 0.0, 60.0)
    if "loop_poll_s" in data:
        cfg["loop_poll_s"] = _float_range(data["loop_poll_s"], "loop_poll_s", 0.01, 60.0)
    if "automation_preset" in data:
        preset = data["automation_preset"]
        if preset not in ("slowed", "fast"):
            _bad_request("automation_preset must be slowed or fast")
        _apply_automation_preset(cfg, preset)
    if "throttle_modulation" in data:
        cfg["throttle_modulation"] = bool(data["throttle_modulation"])
    if "launch_ease" in data:
        cfg["launch_ease"] = bool(data["launch_ease"])
    if "telemetry_enabled" in data:
        cfg["telemetry_enabled"] = bool(data["telemetry_enabled"])
    if "telemetry_host" in data:
        host = data["telemetry_host"]
        if host not in ("localhost", "127.0.0.1"):
            _bad_request("telemetry_host must be localhost or 127.0.0.1")
        cfg["telemetry_host"] = host
    if "telemetry_port" in data:
        cfg["telemetry_port"] = _int_range(data["telemetry_port"], "telemetry_port", 1, 65535)
    if "rich_presence_enabled" in data:
        cfg["rich_presence_enabled"] = bool(data["rich_presence_enabled"])
    if "vision_debug" in data:
        cfg["vision_debug"] = bool(data["vision_debug"])
    return cfg


def _refresh_telemetry(cfg: dict) -> None:
    """Start/refresh/stop the shared telemetry listener to match the config."""
    if cfg.get("telemetry_enabled", True):
        telemetry.shared(cfg.get("telemetry_port", 5300), cfg.get("telemetry_host", "127.0.0.1"))
    else:
        telemetry.stop_shared()


class Bot:
    """Drives core.run in a thread, publishes log/status to SSE subscribers."""

    def __init__(self) -> None:
        self.stop_event: threading.Event | None = None
        self.pause_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.subs: list[queue.Queue] = []
        self.state = "stopped"
        self.laps = 0
        self.started_at: float | None = None
        self.phase_name: str | None = None
        self.phase_started_at: float | None = None

    def _publish(self, ev: dict) -> None:
        for q in list(self.subs):
            q.put(ev)

    def _phase_log(self, name: str, elapsed: float) -> None:
        self._publish({"type": "log", "msg": f"[timing] {name}: {elapsed:.2f}s"})

    def _race_signal_on(self) -> bool:
        t = telemetry.current()
        fresh, race_on, _kmh = t.snapshot() if t else (False, False, 0.0)
        return fresh and race_on

    def _set_phase(self, name: str | None, now: float) -> None:
        self.phase_name = name
        self.phase_started_at = now if name else None

    def _track_phase_timing(self, state: str) -> None:
        now = time.time()
        phase = self.phase_name
        started = self.phase_started_at
        if state == "stopped":
            self._set_phase(None, now)
            return
        if state == "racing":
            if phase == "relaunch" and started is not None:
                if self._race_signal_on():
                    self._phase_log("relance", now - started)
                    self._set_phase("race", now)
                return
            self._set_phase("race", now)
            return
        if state == "results":
            if phase == "race" and started is not None:
                self._phase_log("race", now - started)
            self._set_phase("results", now)
            return
        if state == "restart_confirm":
            if phase == "results" and started is not None:
                self._phase_log("results", now - started)
            self._set_phase("relaunch", now)
            return
        if state == "prerace_menu" and phase in ("results", "restart_confirm"):
            if started is not None:
                self._phase_log(phase, now - started)
            self._set_phase("relaunch", now)

    def on_log(self, msg: str) -> None:
        self._publish({"type": "log", "msg": msg})

    def on_status(self, state: str, laps: int) -> None:
        if state != self.state or (state == "racing" and self.phase_name == "relaunch"):
            self._track_phase_timing(state)
        self.state, self.laps = state, laps
        if state == "stopped":
            self.started_at = None
        elapsed = int(time.time() - self.started_at) if self.started_at else 0
        self._publish({"type": "status", "state": state, "laps": laps, "elapsed_s": elapsed})

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self, max_laps: int) -> bool:
        if self.running:
            return False
        cfg = core.load_config()
        self.started_at = time.time()
        self.stop_event = threading.Event()
        self.pause_event.clear()

        def work() -> None:
            try:
                core.run(
                    cfg, max_cycles=max_laps, stop=self.stop_event, pause=self.pause_event,
                    on_log=self.on_log, on_status=self.on_status,
                )
            except Exception as e:  # surface the error to the frontend
                self.on_log(f"ERROR: {e}")
                self.on_status("stopped", self.laps)
            self._publish({"type": "done"})

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        return True

    def stop(self) -> None:
        if self.stop_event:
            self.stop_event.set()
        self.pause_event.clear()

    def pause(self) -> None:
        self.pause_event.set()

    def resume(self) -> None:
        self.pause_event.clear()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        self.subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        if q in self.subs:
            self.subs.remove(q)


bot = Bot()


def _game_running(name: str) -> bool:
    """True if a process <name> is running (tasklist, no console window)."""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
            capture_output=True, text=True, timeout=4,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
        return name.lower() in out.stdout.lower()
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    server_version = "Cruise"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # silence (no console)
        pass

    # --- helpers ---
    def _check_host(self) -> None:
        # anti DNS-rebinding: a remote site that resolves to 127.0.0.1 will
        # still send its own Host -> rejected. Covers GET and POST.
        host = (self.headers.get("host") or "").lower()
        if host not in ALLOWED_HOSTS:
            raise BadRequest("forbidden host", status=403)

    def _check_local_origin(self, require: bool = False) -> None:
        value = self.headers.get("origin") or self.headers.get("referer")
        if not value:
            # POSTs must carry an Origin/Referer (browsers always send one on
            # fetch POST); silently accepting absence would let origin-less
            # cross-site requests through.
            if require:
                raise BadRequest("missing origin", status=403)
            return
        parsed = urlparse(value)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in ALLOWED_ORIGINS:
            raise BadRequest("forbidden origin", status=403)

    def _check_token(self) -> None:
        token = self.headers.get("x-cruise-token") or ""
        if not hmac.compare_digest(token, _SESSION_TOKEN):
            raise BadRequest("invalid token", status=403)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("content-length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            raise BadRequest("invalid json")
        if not isinstance(data, dict):
            raise BadRequest("json body must be an object")
        return data

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: Path) -> None:
        if not path.is_file():
            self._send_json({"detail": "not found"}, status=404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(data)

    def _static(self, rel: str) -> None:
        # anti-traversal: resolves under WEB_DIR only
        target = (WEB_DIR / rel.lstrip("/")).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._send_json({"detail": "forbidden"}, status=403)
            return
        self._send_file(target)

    def _sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q = bot.subscribe()
        try:
            elapsed = int(time.time() - bot.started_at) if bot.started_at else 0
            init = {"type": "status", "state": bot.state, "laps": bot.laps, "elapsed_s": elapsed}
            self.wfile.write(f"data: {json.dumps(init)}\n\n".encode("utf-8"))
            self.wfile.flush()
            beat = 0
            while True:
                try:
                    ev = q.get(timeout=0.2)
                    self.wfile.write(f"data: {json.dumps(ev)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                except queue.Empty:
                    beat += 1
                    if beat % 50 == 0:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass  # client gone
        finally:
            bot.unsubscribe(q)

    # --- routing ---
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            self._check_host()
            if path == "/":
                self._send_file(WEB_DIR / "index.html")
            elif path.startswith("/static/"):
                self._static(path[len("/static/"):])
            elif path == "/api/session":
                self._send_json({"token": _SESSION_TOKEN})
            elif path == "/api/config":
                self._send_json(core.load_config())
            elif path == "/api/gamepad-check":
                ok, msg = inputs.gamepad_available()
                self._send_json({"ok": ok, "message": msg})
            elif path == "/api/game-status":
                cfg = core.load_config()
                name = cfg.get("game_process", "forzahorizon6.exe")
                self._send_json({
                    "running": _game_running(name),
                    "process": name,
                    "display": core.display_status(cfg),
                })
            elif path == "/api/telemetry":
                cfg = core.load_config()
                t = telemetry.current()
                fresh, race_on, kmh = t.snapshot() if t else (False, False, 0.0)
                self._send_json({
                    "enabled": cfg.get("telemetry_enabled", True),
                    "host": cfg.get("telemetry_host", "127.0.0.1"),
                    "port": cfg.get("telemetry_port", 5300),
                    "available": fresh,
                    "race_on": race_on,
                    "speed_kmh": round(kmh, 1),
                })
            elif path == "/api/rich-presence":
                cfg = core.load_config()
                rp = discord_presence.current()
                disp = rp.display() if rp else {"car": None, "state": None}
                self._send_json({
                    "enabled": cfg.get("rich_presence_enabled", True),
                    "connected": bool(rp and rp.connected),
                    "car": disp["car"],
                    "state": disp["state"],
                })
            elif path == "/api/status":
                self._send_json({"state": bot.state, "laps": bot.laps, "running": bot.running})
            elif path == "/api/events":
                self._sse()
            else:
                self._send_json({"detail": "not found"}, status=404)
        except BadRequest as e:
            self._send_json({"detail": e.message}, status=e.status)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            self._check_host()
            self._check_local_origin(require=True)
            self._check_token()
            if path == "/api/config":
                cfg = _apply_config_update(core.load_config(), self._read_json())
                with core.CONFIG_PATH.open("w", encoding="utf-8") as f:
                    json.dump(cfg, f, indent=2, ensure_ascii=False)
                _refresh_telemetry(cfg)
                self._send_json({"ok": True, "config": cfg})
            elif path == "/api/gamepad-connect":
                self._send_json({"ok": inputs.connect_gamepad()})
            elif path == "/api/gamepad-disconnect":
                inputs.disconnect_gamepad()
                self._send_json({"ok": True})
            elif path == "/api/start":
                data = self._read_json()
                max_laps = _int_range(data.get("max_laps", 0), "max_laps", 0, 1000000)
                started = bot.start(max_laps)
                self._send_json({"started": started, "running": bot.running})
            elif path == "/api/stop":
                bot.stop()
                self._send_json({"ok": True})
            elif path == "/api/pause":
                bot.pause()
                self._send_json({"ok": True, "paused": True})
            elif path == "/api/resume":
                bot.resume()
                self._send_json({"ok": True, "paused": False})
            else:
                self._send_json({"detail": "not found"}, status=404)
        except BadRequest as e:
            self._send_json({"detail": e.message}, status=e.status)


def make_server() -> ThreadingHTTPServer:
    try:
        cfg = core.load_config()
        _refresh_telemetry(cfg)  # live telemetry for the UI (browser + desktop)
        discord_presence.shared(  # Discord Rich Presence (FH6 car + speed/gear)
            cfg.get("discord_client_id", discord_presence.DEFAULT_CLIENT_ID),
            core.CONFIG_PATH.parent,
            core.load_config,  # read live config (enabled flag + game_process)
        )
    except Exception:
        pass
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    return srv


def main() -> None:
    if already_running():
        # Another instance is up: just surface its UI, don't start a second server.
        webbrowser.open(f"http://{HOST}:{PORT}")
        return
    srv = make_server()
    threading.Timer(1.2, lambda: webbrowser.open(f"http://{HOST}:{PORT}")).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()


if __name__ == "__main__":
    main()
