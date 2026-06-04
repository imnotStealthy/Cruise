"""Game window detection and control (Windows).

Finds the REAL Forza Horizon 6 render window (exact title -> process -> title
contains, ~16:9 aspect filter), gives its client area in screen coords, focus,
and display state. Everything is relative to the window -> compatible with
windowed, borderless and multi-monitor.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

import screen


def _enum_game_windows():
    """[(hwnd, title, pid, (x,y,w,h)), ...] of visible windows with non-empty client."""
    if not hasattr(ctypes, "windll"):
        return []
    user32 = ctypes.windll.user32
    out = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value
        rect = wintypes.RECT()
        if user32.GetClientRect(hwnd, ctypes.byref(rect)):
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w > 0 and h > 0:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                pt = wintypes.POINT(0, 0)
                user32.ClientToScreen(hwnd, ctypes.byref(pt))
                out.append((hwnd, title, pid.value, (pt.x, pt.y, w, h)))
        return True

    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb)
    user32.EnumWindows(proc, 0)
    return out


def _pid_image(pid: int) -> str:
    """Executable name (basename) of process pid, or ''."""
    try:
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buf = ctypes.create_unicode_buffer(size.value)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                return buf.value.rsplit("\\", 1)[-1]
            return ""
        finally:
            k.CloseHandle(h)
    except Exception:
        return ""


def _aspect_ok(rect: tuple[int, int, int, int]) -> bool:
    w, h = rect[2], rect[3]
    return h > 0 and 1.4 <= (w / h) <= 2.4  # ~16:10..21:9, excludes aberrant windows


def _client_rect(hwnd) -> tuple[int, int, int, int] | None:
    """Client area (x,y,w,h) in screen coords of a hwnd, or None."""
    user32 = ctypes.windll.user32
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None
    pt = wintypes.POINT(0, 0)
    user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return (pt.x, pt.y, w, h)


def _window_alive(hwnd) -> bool:
    user32 = ctypes.windll.user32
    return bool(user32.IsWindow(hwnd) and user32.IsWindowVisible(hwnd))


# Cache of the selected window: EnumWindows + QueryFullProcessImageName on
# each poll is costly. We revalidate the cached hwnd (cheap) and only redo a
# full scan if it has disappeared/hidden.
_cache = None


def select_game_window(cfg: dict):
    """Picks the REAL FH6 render window: (hwnd, title, pid, (x,y,w,h)) or None.
    Priority: exact title -> exact process -> title contains. ~16:9 aspect filter,
    then largest area (avoids parasitic/overlay windows with the same title)."""
    global _cache
    if _cache is not None and hasattr(ctypes, "windll") and _window_alive(_cache[0]):
        rect = _client_rect(_cache[0])  # refresh the position (window moved)
        if rect:
            _cache = (_cache[0], _cache[1], _cache[2], rect)
            return _cache
    win = _full_select(cfg)
    _cache = win
    return win


def _full_select(cfg: dict):
    wins = _enum_game_windows()
    if not wins:
        return None
    title = cfg.get("game_window_title", "Forza Horizon 6").lower()
    proc = cfg.get("game_process", "forzahorizon6.exe").lower()

    exact = [w for w in wins if w[1].lower() == title]
    byproc = exact or [w for w in wins if _pid_image(w[2]).lower() == proc]
    contains = byproc or [w for w in wins if title in w[1].lower()]
    group = contains
    if not group:
        return None
    ok = [w for w in group if _aspect_ok(w[3])] or group
    ok.sort(key=lambda w: w[3][2] * w[3][3], reverse=True)
    return ok[0]


def game_client_rect(cfg: dict):
    """Client area (x,y,w,h) of the FH6 render window, or None."""
    win = select_game_window(cfg)
    return win[3] if win else None


def is_foreground(win) -> bool:
    """True if the selected window is in the foreground (has focus)."""
    if not hasattr(ctypes, "windll"):
        return True
    if not win:
        return False
    return int(ctypes.windll.user32.GetForegroundWindow()) == int(win[0])


def focus_game_window(cfg: dict) -> bool:
    """Brings FH6 to the foreground -> inputs go to the game (not the app).
    Allows accelerating immediately without a manual delay."""
    win = select_game_window(cfg)
    if not win or not hasattr(ctypes, "windll"):
        return False
    user32 = ctypes.windll.user32
    hwnd = win[0]
    try:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE (if minimized)
        # AttachThreadInput to bypass the Windows focus-stealing block
        fg = user32.GetForegroundWindow()
        cur = user32.GetWindowThreadProcessId(fg, None)
        tgt = user32.GetWindowThreadProcessId(hwnd, None)
        user32.AttachThreadInput(cur, tgt, True)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(cur, tgt, False)
        return True
    except Exception:
        return False


def detection_rect(cfg: dict) -> tuple[int, int, int, int]:
    """Reference area for fractional coords: FH6 window if found
    (handles windowed mode), otherwise full screen."""
    rect = game_client_rect(cfg)
    if rect:
        return rect
    w, h = screen.size()
    return (0, 0, w, h)


def display_status(cfg: dict) -> dict:
    """Game display state for the UI: window found? fullscreen?"""
    rect = game_client_rect(cfg)
    if not rect:
        return {"found": False, "fullscreen": None, "rect": None}
    ox, oy, w, h = rect
    sw, sh = screen.size()
    fullscreen = abs(ox) <= 1 and abs(oy) <= 1 and abs(w - sw) <= 2 and abs(h - sh) <= 2
    return {"found": True, "fullscreen": fullscreen, "rect": {"x": ox, "y": oy, "w": w, "h": h}}
