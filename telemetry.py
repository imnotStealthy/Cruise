"""Forza Horizon "Data Out" UDP telemetry reader (speed + race state).

Enable in-game: Settings -> HUD and Gameplay -> Data Out = ON,
  Data Out IP = 127.0.0.1, Data Out Port = telemetry_port (config, default 5300).

The bot uses real speed to tell a genuine stop (stuck against a wall, ~0 km/h)
from a jump or off-road excursion (still moving fast) -> it no longer rewinds in
mid-air just because the sampled pixels (sky) stopped changing.

Only the V1 "Sled" header is parsed — its layout is identical across every Forza
format (Motorsport + Horizon), so it is robust to FH dash-packet differences:
  offset 0  : IsRaceOn      (s32, little-endian)
  offset 32 : Velocity X/Y/Z (3x f32)  -> speed = |v| in m/s.
"""
from __future__ import annotations

import socket
import struct
import threading
import time

_SLED_MIN = 44  # need bytes 32..43 (VelocityZ ends at 44)


class Telemetry:
    """Background UDP listener. Non-blocking; snapshot() returns the latest sample
    and whether it is recent enough to trust (else the caller falls back to the
    visual stuck detection)."""

    def __init__(self, port: int, host: str = "0.0.0.0") -> None:
        self.port = int(port)
        self.host = host
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._speed_ms = 0.0
        self._race_on = False
        self._gear: int | None = None
        self._car_ordinal = 0
        self._ts = 0.0  # wall time of the last parsed packet

    def start(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((self.host, self.port))
            s.settimeout(0.5)
        except OSError:
            return False
        self._sock = s
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < _SLED_MIN:
                continue
            n = len(data)
            try:
                race_on = struct.unpack_from("<i", data, 0)[0]
                vx, vy, vz = struct.unpack_from("<fff", data, 32)
                speed_ms = (vx * vx + vy * vy + vz * vz) ** 0.5
                # CarOrdinal lives in the V1 Sled block (robust). Gear lives in
                # the Horizon "Dash" block (offset 307 + 12-byte Horizon gap).
                car_ordinal = struct.unpack_from("<i", data, 212)[0] if n >= 216 else 0
                gear = data[319] if n >= 320 else None
                if n >= 260:  # Dash speed (m/s) is more accurate than |velocity|
                    dash_speed = struct.unpack_from("<f", data, 256)[0]
                    if dash_speed > 0.0:
                        speed_ms = dash_speed
            except struct.error:
                continue
            with self._lock:
                self._race_on = bool(race_on)
                self._speed_ms = speed_ms
                self._gear = gear
                self._car_ordinal = car_ordinal
                self._ts = time.time()

    def snapshot(self, max_age: float = 1.0) -> tuple[bool, bool, float]:
        """(fresh, race_on, speed_kmh). fresh=False if no packet within max_age
        seconds (Data Out off / wrong port) -> caller should fall back to vision."""
        with self._lock:
            fresh = self._ts > 0.0 and (time.time() - self._ts) <= max_age
            return fresh, self._race_on, self._speed_ms * 3.6

    def details(self, max_age: float = 2.0) -> tuple[bool, bool, float, int | None, int]:
        """(fresh, race_on, speed_kmh, gear, car_ordinal) for the rich presence."""
        with self._lock:
            fresh = self._ts > 0.0 and (time.time() - self._ts) <= max_age
            return fresh, self._race_on, self._speed_ms * 3.6, self._gear, self._car_ordinal

    def close(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass


# Process-wide singleton: the server (live UI) and the bot loop run in the SAME
# process and would otherwise both bind the same UDP port (only one would then
# receive). They share this one listener instead.
_shared: Telemetry | None = None


def shared(port: int, host: str = "127.0.0.1") -> Telemetry | None:
    """Get/start the shared listener, rebinding if port/host changed. None on
    bind failure."""
    global _shared
    port = int(port)
    if _shared is not None and (_shared.port != port or _shared.host != host):
        _shared.close()
        _shared = None
    if _shared is None:
        t = Telemetry(port, host)
        if not t.start():
            return None
        _shared = t
    return _shared


def current() -> Telemetry | None:
    """The shared listener if started, else None (no side effects)."""
    return _shared


def stop_shared() -> None:
    global _shared
    if _shared is not None:
        _shared.close()
        _shared = None
