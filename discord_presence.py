"""Discord Rich Presence for Cruise.

Shows the live FH6 car + speed/gear in the user's Discord status, driven by the
telemetry listener. Talks to Discord over its local IPC named pipe directly
(no external dependency): handshake (op 0) then SET_ACTIVITY frames (op 1),
each framed as <op u32 LE><len u32 LE><json>. Same client id + "forza" asset as
the user's own Discord application.

Car names come from a small ordinal->name database (cars.json), cached locally
and refreshed once from the public DB (best-effort, offline-safe).
"""
from __future__ import annotations

import ctypes
import json
import os
import struct
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path

import telemetry

DEFAULT_CLIENT_ID = "1508465470658052208"
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_EPOCH_AS_FILETIME = 11644473600  # seconds between 1601-01-01 and 1970-01-01


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
    ]


def _find_pid(name: str) -> int | None:
    """First PID whose image name matches `name` (case-insensitive), or None."""
    k = ctypes.windll.kernel32
    snap = k.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snap == -1 or snap == ctypes.c_void_p(-1).value:
        return None
    try:
        entry = _PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        target = name.lower()
        if not k.Process32FirstW(snap, ctypes.byref(entry)):
            return None
        while True:
            if entry.szExeFile.lower() == target:
                return entry.th32ProcessID
            if not k.Process32NextW(snap, ctypes.byref(entry)):
                return None
    finally:
        k.CloseHandle(snap)


def _process_start_unix(pid: int) -> int | None:
    """Process creation time as a unix timestamp, or None."""
    k = ctypes.windll.kernel32
    h = k.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        creation, exit_t, kern, user = (wintypes.FILETIME() for _ in range(4))
        if not k.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_t),
                                 ctypes.byref(kern), ctypes.byref(user)):
            return None
        ft = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return int(ft / 10_000_000 - _EPOCH_AS_FILETIME)
    finally:
        k.CloseHandle(h)
CAR_DB_URL = (
    "https://raw.githubusercontent.com/1Stalk/"
    "Forza-Horizon-Discord-Rich-Presence/main/src-tauri/cars.json"
)


class _CarNames:
    """ordinal(str) -> car name. Local cars.json cache, refreshed once from the
    remote DB (best-effort; failure just means generic names)."""

    def __init__(self, base: Path) -> None:
        self.path = base / "cars.json"
        # bundled copy inside the exe -> works when only Cruise.exe is shipped
        self.bundled = Path(getattr(sys, "_MEIPASS", str(base))) / "cars.json"
        self._map: dict | None = None

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    def _seed_from_bundle(self) -> dict | None:
        data = self._read(self.bundled)
        if data:
            try:
                self.path.write_text(json.dumps(data), encoding="utf-8")
            except Exception:
                pass
        return data

    def _fetch_remote(self) -> dict | None:
        try:
            with urllib.request.urlopen(CAR_DB_URL, timeout=4) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        try:
            self.path.write_text(json.dumps(data), encoding="utf-8")
        except Exception:
            pass
        return data

    def name(self, ordinal: int) -> str | None:
        if not ordinal:
            return None
        if self._map is None:
            # local cache -> bundled default -> remote DB (offline-safe order)
            self._map = self._read(self.path) or self._seed_from_bundle() or self._fetch_remote() or {}
        return self._map.get(str(ordinal))


class DiscordPresence:
    """Background thread that mirrors telemetry into a Discord activity."""

    def __init__(self, client_id: str, base: Path, get_config) -> None:
        self.client_id = str(client_id)
        self.cars = _CarNames(Path(base))
        self._get_config = get_config  # callable() -> config dict, read live
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pipe = None
        self._connected = False
        self._app_started = int(time.time())     # fallback if FH6 PID not found
        self._game_pid: int | None = None
        self._game_started: int | None = None
        self._last_car: str | None = None
        self._last_payload: str | None = None

    def _is_enabled(self) -> bool:
        return bool(self._get_config().get("rich_presence_enabled", True))

    def _interval(self) -> float:
        """Seconds between activity refreshes. Floored at Discord's rate limit
        (SET_ACTIVITY = 5 per 20 s = one every 4 s) so we update as fast as
        allowed without being throttled."""
        try:
            return max(4.0, float(self._get_config().get("rich_presence_interval_s", 4.0)))
        except (TypeError, ValueError):
            return 4.0

    def _session_start(self) -> int:
        """Timer anchor = the FH6 game session start (process creation time),
        cached per-PID so it survives Cruise restarts and only resets when the
        game itself is relaunched. Falls back to the app start time."""
        name = self._get_config().get("game_process", "forzahorizon6.exe")
        pid = _find_pid(name)
        if pid is not None:
            if pid == self._game_pid and self._game_started is not None:
                return self._game_started
            started = _process_start_unix(pid)
            if started is not None:
                self._game_pid, self._game_started = pid, started
                return started
        return self._game_started or self._app_started

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._clear_and_close()

    @property
    def connected(self) -> bool:
        return self._connected

    # --- IPC ---
    def _frame(self, op: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._pipe.write(struct.pack("<II", op, len(body)) + body)
        self._pipe.flush()

    def _connect(self) -> bool:
        for i in range(10):
            try:
                pipe = open(r"\\.\pipe\discord-ipc-%d" % i, "r+b", buffering=0)
            except OSError:
                continue
            self._pipe = pipe
            try:
                self._frame(0, {"v": 1, "client_id": self.client_id})  # handshake
                self._connected = True
                return True
            except OSError:
                try:
                    pipe.close()
                except OSError:
                    pass
                self._pipe = None
        return False

    def _set_activity(self, activity) -> bool:
        try:
            self._frame(1, {
                "cmd": "SET_ACTIVITY",
                "args": {"pid": os.getpid(), "activity": activity},
                "nonce": "%.7f" % time.time(),
            })
            return True
        except OSError:
            self._clear_and_close()
            return False

    def _clear_and_close(self) -> None:
        if self._pipe is not None:
            try:
                self._frame(1, {
                    "cmd": "SET_ACTIVITY",
                    "args": {"pid": os.getpid(), "activity": None},
                    "nonce": "%.7f" % time.time(),
                })
            except OSError:
                pass
            try:
                self._pipe.close()
            except OSError:
                pass
        self._pipe = None
        self._connected = False
        self._last_payload = None

    # --- activity payload ---
    def _activity(self):
        t = telemetry.current()
        if t is None:
            return None
        fresh, race_on, kmh, gear, ordinal = t.details()
        if not fresh:
            return None
        named = self.cars.name(ordinal)
        if named:
            self._last_car = named
        car = named or self._last_car or "Forza Horizon 6"
        # IsRaceOn = 0 means paused or sitting in a menu (Forza zeroes speed/gear
        # then) -> show that instead of a misleading "0 km/h - Gear 0".
        if race_on:
            speed = max(0, round(kmh))
            state = f"{speed} km/h - Gear {gear}" if gear is not None else f"{speed} km/h"
        else:
            state = "In the menus"
        return {
            "details": car,
            "state": state,
            "assets": {"large_image": "forza", "large_text": "Forza Horizon 6"},
            "timestamps": {"start": self._session_start()},
        }

    def display(self) -> dict:
        """Read-only view of what is/would be shown, for the UI."""
        a = self._activity()
        return {"car": a["details"], "state": a["state"]} if a else {"car": None, "state": None}

    def _run(self) -> None:
        while not self._stop.is_set():
            if not self._is_enabled():
                if self._pipe is not None:
                    self._clear_and_close()
                self._stop.wait(2)
                continue
            if self._pipe is None and not self._connect():
                self._stop.wait(5)
                continue
            activity = self._activity()
            if activity is None:
                if self._last_payload is not None:
                    self._set_activity(None)
                    self._last_payload = None
                self._stop.wait(5)
                continue
            payload = json.dumps(activity, sort_keys=True)
            if payload != self._last_payload and self._set_activity(activity):
                self._last_payload = payload
            self._stop.wait(self._interval())


_shared: DiscordPresence | None = None


def shared(client_id: str, base, get_config) -> DiscordPresence:
    """Get/start the process-wide presence thread. get_config() -> config dict."""
    global _shared
    if _shared is None:
        _shared = DiscordPresence(client_id, Path(base), get_config)
        _shared.start()
    return _shared


def current() -> DiscordPresence | None:
    return _shared


def stop_shared() -> None:
    global _shared
    if _shared is not None:
        _shared.stop()
        _shared = None
