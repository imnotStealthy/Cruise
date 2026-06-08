"""Native window launcher (PyWebView) — same frontend, no browser.

The local http server runs internally on 127.0.0.1 (invisible, never exposed),
and the UI is displayed in a native app window (WebView2 on Windows).

Run: py desktop.py
"""
from __future__ import annotations

import os

# Make Cruise capturable by OBS' classic "Window Capture" (BitBlt). WebView2 is
# GPU-accelerated and presents via DirectComposition, which BitBlt renders as a
# black box. Cruise's UI is a tiny static page that needs no GPU, so force the
# legacy (software, no-DComp) presentation path. --disable-direct-composition is
# the key flag that makes BitBlt see the content.
_OBS_FLAGS = "--disable-gpu --disable-gpu-compositing --disable-direct-composition"
os.environ.setdefault("WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS", _OBS_FLAGS)

import socket
import threading
import time
import webbrowser

import webview


def _force_obs_capturable() -> None:
    """pywebview hard-codes WebView2's AdditionalBrowserArguments, which overrides
    the env var above. Append our flags onto whatever it sets so the window stays
    BitBlt-capturable. Patches our own runtime, not the installed package."""
    try:
        import webview.platforms.edgechromium as ec
    except Exception:
        return
    orig = ec.EdgeChrome.__init__
    if getattr(orig, "_cruise_patched", False):
        return

    def patched(self, *args, **kwargs):
        orig(self, *args, **kwargs)
        try:
            props = self.webview.CreationProperties
            cur = props.AdditionalBrowserArguments or ""
            if "--disable-direct-composition" not in cur:
                props.AdditionalBrowserArguments = (cur + " " + _OBS_FLAGS).strip()
                self.webview.CreationProperties = props
        except Exception:
            pass

    patched._cruise_patched = True
    ec.EdgeChrome.__init__ = patched


_force_obs_capturable()

import inputs
import server

_SRV = None


def _run_server() -> None:
    global _SRV
    _SRV = server.make_server()
    _SRV.serve_forever()


def _wait_for_server(timeout: float = 12.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            if s.connect_ex((server.HOST, server.PORT)) == 0:
                return True
        time.sleep(0.2)
    return False


def main() -> None:
    if server.already_running():
        webbrowser.open(f"http://{server.HOST}:{server.PORT}")
        return  # single instance: a Cruise window is already open
    threading.Thread(target=_run_server, daemon=True).start()
    _wait_for_server()

    window = webview.create_window(
        "Cruise",
        f"http://{server.HOST}:{server.PORT}",
        width=940,
        height=820,
        min_size=(700, 640),
        background_color="#070905",
    )

    def _on_closing() -> None:
        # clean bot stop (releases key/gamepad) before closing
        try:
            server.bot.stop()
            time.sleep(0.4)
            inputs.disconnect_gamepad()
        except Exception:
            pass

    window.events.closing += _on_closing
    webview.start()


if __name__ == "__main__":
    main()
