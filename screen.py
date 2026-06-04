"""Low-level screen reading (Windows GDI) — replaces pyautogui/Pillow.

The bot only needs to: read a pixel's color, the screen size, and a
"mouse in a corner" failsafe. Everything goes through ctypes (user32/gdi32), no
heavy dependency (Pillow ~16 MB saved in the build).
"""
from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes

_user32 = ctypes.windll.user32
_gdi32 = ctypes.windll.gdi32


def _set_dpi_aware() -> None:
    """Make the process DPI-aware so GetClientRect/ClientToScreen/GetPixel use
    PHYSICAL pixels (matching how FH6 renders). Without this, on a display scaled
    != 100% a DPI-unaware process reads VIRTUALIZED window coords: fullscreen
    still works (offset ~0,0 + proportional sampling hides the error) but
    WINDOWED mode samples the wrong pixels (non-zero offset is mis-scaled) ->
    states like 'results' are never detected and the bot never restarts."""
    for attempt in (
        lambda: _user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)),  # Win10 1703+: per-monitor v2
        lambda: ctypes.windll.shcore.SetProcessDpiAwareness(2),              # Win 8.1+: per-monitor
        lambda: _user32.SetProcessDPIAware(),                                # Vista+: system
    ):
        try:
            attempt()
            return
        except Exception:
            continue


_set_dpi_aware()

# Explicit signatures: essential on 64-bit (HDC = pointer, otherwise truncated).
_user32.GetDC.restype = wintypes.HDC
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.ReleaseDC.restype = ctypes.c_int
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_gdi32.GetPixel.restype = wintypes.COLORREF
_gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]

# Region capture (BitBlt + GetDIBits) for the speed-OCR. Pure ctypes, no Pillow.
_gdi32.CreateCompatibleDC.restype = wintypes.HDC
_gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
_gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
_gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
_gdi32.SelectObject.restype = wintypes.HGDIOBJ
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
_gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                          ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
_gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
_gdi32.DeleteDC.argtypes = [wintypes.HDC]
_gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, ctypes.c_uint, ctypes.c_uint,
                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]

_CLR_INVALID = 0xFFFFFFFF
_SRCCOPY = 0x00CC0020


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD),
    ]


def grab_bgra(rect: tuple[int, int, int, int]):
    """Capture a screen region (x, y, w, h). Returns (w, h, buf) where buf is a
    flat BGRA byte array (4 bytes/pixel, top-down rows). No Pillow."""
    x, y, w, h = (int(v) for v in rect)
    if w <= 0 or h <= 0:
        return (0, 0, None)
    src = _user32.GetDC(0)
    mem = _gdi32.CreateCompatibleDC(src)
    bmp = _gdi32.CreateCompatibleBitmap(src, w, h)
    old = _gdi32.SelectObject(mem, bmp)
    try:
        _gdi32.BitBlt(mem, 0, 0, w, h, src, x, y, _SRCCOPY)
        bi = _BITMAPINFOHEADER()
        bi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bi.biWidth = w
        bi.biHeight = -h  # negative -> top-down rows
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0  # BI_RGB
        buf = (ctypes.c_ubyte * (w * h * 4))()
        _gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
    finally:
        _gdi32.SelectObject(mem, old)
        _gdi32.DeleteObject(bmp)
        _gdi32.DeleteDC(mem)
        _user32.ReleaseDC(0, src)
    return (w, h, buf)


def grab_luma(rect: tuple[int, int, int, int]) -> tuple[int, int, list[int]]:
    """Capture a screen region (x, y, w, h) and return (w, h, luma[]) where luma is
    a row-major list of 0-255 brightness values. Top-down rows. No Pillow."""
    w, h, buf = grab_bgra(rect)
    if w == 0:
        return (0, 0, [])
    luma = [(buf[i * 4 + 2] * 77 + buf[i * 4 + 1] * 150 + buf[i * 4] * 29) >> 8 for i in range(w * h)]
    return (w, h, luma)


def grab_white(rect: tuple[int, int, int, int], min_v: int = 200, sat_tol: int = 45) -> tuple[int, int, list[int]]:
    """Capture and return (w, h, mask[]) where mask is 1 for near-WHITE pixels
    (all channels >= min_v AND low saturation). Isolates white HUD text from bright
    but coloured backgrounds (blue sky, warm sun), unlike a plain luma threshold."""
    w, h, buf = grab_bgra(rect)
    if w == 0:
        return (0, 0, [])
    mask = [0] * (w * h)
    for i in range(w * h):
        b = buf[i * 4]
        g = buf[i * 4 + 1]
        r = buf[i * 4 + 2]
        lo = b if b < g else g
        lo = lo if lo < r else lo
        hi = b if b > g else g
        hi = hi if hi > r else hi
        if lo >= min_v and (hi - lo) <= sat_tol:
            mask[i] = 1
    return (w, h, mask)


class FailSafeException(Exception):
    """Mouse moved into a screen corner -> emergency stop (like pyautogui)."""


def size() -> tuple[int, int]:
    return (_user32.GetSystemMetrics(0), _user32.GetSystemMetrics(1))


# Shared DC for a poll: opening/closing a Device Context for each pixel is
# costly (~9 GetDC/poll with 3 states x 3 pixels). dc_session() opens a single
# one, reused by pixel() for the duration of the block.
_session_dc = None


@contextlib.contextmanager
def dc_session():
    """Keeps a screen DC open for the duration of the block -> pixel() reuses it."""
    global _session_dc
    _session_dc = _user32.GetDC(0)
    try:
        yield
    finally:
        if _session_dc:
            _user32.ReleaseDC(0, _session_dc)
        _session_dc = None


def pixel(x: int, y: int) -> tuple[int, int, int]:
    hdc = _session_dc or _user32.GetDC(0)
    if not hdc:
        return (0, 0, 0)
    owns = _session_dc is None
    try:
        color = _gdi32.GetPixel(hdc, int(x), int(y))  # 0x00BBGGRR
    finally:
        if owns:
            _user32.ReleaseDC(0, hdc)
    if color == _CLR_INVALID:
        return (0, 0, 0)
    return (color & 0xFF, (color >> 8) & 0xFF, (color >> 16) & 0xFF)


def motion_signature(rect: tuple[int, int, int, int], points) -> tuple[int, ...]:
    """Sample fractional points (fx, fy) inside rect and return a per-point
    brightness tuple. Comparing two signatures detects whether the scene moves:
    a stuck car (against a wall/vehicle) yields a near-constant signature, while
    driving constantly changes road/scenery pixels."""
    ox, oy, w, h = rect
    with dc_session():
        return tuple(sum(pixel(ox + int(fx * w), oy + int(fy * h))) for fx, fy in points)


def signature_delta(a, b) -> int:
    """Total absolute difference between two signatures (0 = identical)."""
    if a is None or b is None or len(a) != len(b):
        return 1 << 30
    return sum(abs(x - y) for x, y in zip(a, b))


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def check_failsafe() -> None:
    """Raises FailSafeException if the cursor is in a corner (1 px tolerance)."""
    x, y = cursor_pos()
    w, h = size()
    corner = (
        (x <= 0 and y <= 0)
        or (x >= w - 1 and y <= 0)
        or (x <= 0 and y >= h - 1)
        or (x >= w - 1 and y >= h - 1)
    )
    if corner:
        raise FailSafeException("mouse in corner")
