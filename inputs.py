"""Input abstraction: keyboard (pydirectinput) or emulated Xbox gamepad (vgamepad/ViGEm).

The bot reasons in logical actions ("enter", "x", accelerate). Each backend
translates them: keyboard -> keys; gamepad -> Xbox buttons / right trigger.
"""
from __future__ import annotations

import time


class InputBackend:
    name = "base"

    def hold_accelerate(self) -> None: ...
    def reassert_accelerate(self) -> None:
        """Force the accelerate input down again. Re-pressed every poll so a stray
        manual key press/release (the user bumping W) can't desync the hold."""
        self.hold_accelerate()
    def release_accelerate(self) -> None: ...
    def hold_steer(self) -> None: ...
    def release_steer(self) -> None: ...
    def tap(self, action: str) -> None: ...
    def recover(self, direction: str, reverse_s: float = 1.0, steer_s: float = 0.8) -> None:
        """Unstick maneuver: back up while turning, then drive forward turning to
        clear the obstacle. direction = 'left' | 'right'. Leaves accelerate held."""
    def rewind(self) -> None:
        """Trigger the game's Rewind once (snaps the car back onto the track in the
        right direction). Far more reliable than a blind reverse+steer."""
    def close(self) -> None: ...


class KeyboardBackend(InputBackend):
    name = "keyboard"

    def __init__(self, accel_key: str = "w", steer_key: str | None = None,
                 reverse_key: str = "s", left_key: str = "a", right_key: str = "d",
                 rewind_key: str = "r") -> None:
        import pydirectinput
        pydirectinput.PAUSE = 0.0
        self._pdi = pydirectinput
        self.accel = accel_key
        self.steer = steer_key
        self.reverse_key = reverse_key
        self.left_key = left_key
        self.right_key = right_key
        self.rewind_key = rewind_key
        self._accel_down = False
        self._steer_down = False

    def hold_accelerate(self) -> None:
        if not self._accel_down:
            self._pdi.keyDown(self.accel)
            self._accel_down = True

    def reassert_accelerate(self) -> None:
        # Re-press unconditionally: a stray physical key-up (user bumping W) would
        # otherwise release it while _accel_down stays True -> car silently stops.
        self._pdi.keyDown(self.accel)
        self._accel_down = True

    def release_accelerate(self) -> None:
        if self._accel_down:
            self._pdi.keyUp(self.accel)
            self._accel_down = False

    def hold_steer(self) -> None:
        if self.steer and not self._steer_down:
            self._pdi.keyDown(self.steer)
            self._steer_down = True

    def release_steer(self) -> None:
        if self.steer and self._steer_down:
            self._pdi.keyUp(self.steer)
            self._steer_down = False

    def tap(self, action: str) -> None:
        self._pdi.press(action)

    def recover(self, direction: str, reverse_s: float = 1.0, steer_s: float = 0.8) -> None:
        sd = self.left_key if direction == "left" else self.right_key
        self.release_accelerate()
        self.release_steer()
        # back up while turning away from the obstacle
        self._pdi.keyDown(self.reverse_key)
        self._pdi.keyDown(sd)
        time.sleep(reverse_s)
        self._pdi.keyUp(self.reverse_key)
        # then drive forward, still turning, to clear it
        self._pdi.keyDown(self.accel)
        self._accel_down = True
        time.sleep(steer_s)
        self._pdi.keyUp(sd)

    def rewind(self) -> None:
        self.release_accelerate()
        self.release_steer()
        self._pdi.press(self.rewind_key)

    def close(self) -> None:
        self.release_accelerate()
        self.release_steer()


# PERSISTENT virtual gamepad: created once and kept alive for the entire
# process lifetime. Otherwise FH6 shows connected/disconnected on each
# Start/Stop (the object was recreated/destroyed on each run).
_SHARED_PAD = None


def _get_shared_pad():
    global _SHARED_PAD
    if _SHARED_PAD is None:
        import vgamepad as vg
        _SHARED_PAD = vg.VX360Gamepad()
    return _SHARED_PAD


def disconnect_gamepad() -> None:
    """Disconnects the virtual gamepad (to call when the app closes)."""
    global _SHARED_PAD
    if _SHARED_PAD is not None:
        try:
            _SHARED_PAD.reset()
            _SHARED_PAD.update()
        except Exception:
            pass
        _SHARED_PAD = None  # GC -> ViGEm disconnect


class GamepadBackend(InputBackend):
    """Persistent virtual Xbox360 gamepad. Accelerate = right trigger (RT)."""
    name = "gamepad"

    def __init__(self, steer_key: str | None = None) -> None:
        import vgamepad as vg
        self._vg = vg
        self.pad = _get_shared_pad()
        B = vg.XUSB_BUTTON
        self.button_map = {
            "enter": B.XUSB_GAMEPAD_A,
            "return": B.XUSB_GAMEPAD_A,
            "a": B.XUSB_GAMEPAD_A,
            "b": B.XUSB_GAMEPAD_B,
            "x": B.XUSB_GAMEPAD_X,
            "y": B.XUSB_GAMEPAD_Y,
            "esc": B.XUSB_GAMEPAD_B,
            "space": B.XUSB_GAMEPAD_BACK,
        }
        self._default = B.XUSB_GAMEPAD_A
        self._accel_down = False

    def hold_accelerate(self) -> None:
        if not self._accel_down:
            self.pad.right_trigger(value=255)
            self.pad.update()
            self._accel_down = True

    def reassert_accelerate(self) -> None:
        self.pad.right_trigger(value=255)
        self.pad.update()
        self._accel_down = True

    def release_accelerate(self) -> None:
        if self._accel_down:
            self.pad.right_trigger(value=0)
            self.pad.update()
            self._accel_down = False

    def hold_steer(self) -> None:  # auto-steering handles direction in-game
        pass

    def release_steer(self) -> None:
        pass

    def tap(self, action: str) -> None:
        btn = self.button_map.get(action.lower(), self._default)
        self.pad.press_button(button=btn)
        self.pad.update()
        time.sleep(0.08)
        self.pad.release_button(button=btn)
        self.pad.update()

    def recover(self, direction: str, reverse_s: float = 1.0, steer_s: float = 0.8) -> None:
        x = -32768 if direction == "left" else 32767
        self.release_accelerate()
        self.pad.left_trigger(value=255)  # reverse / brake
        self.pad.left_joystick(x_value=x, y_value=0)
        self.pad.update()
        time.sleep(reverse_s)
        self.pad.left_trigger(value=0)
        self.pad.right_trigger(value=255)  # forward
        self._accel_down = True
        self.pad.update()
        time.sleep(steer_s)
        self.pad.left_joystick(x_value=0, y_value=0)
        self.pad.update()

    def rewind(self) -> None:
        self.release_accelerate()
        self.tap("y")  # FH6 Rewind = Y on the controller

    def close(self) -> None:
        self.release_accelerate()
        self.pad.reset()
        self.pad.update()


def gamepad_available() -> tuple[bool, str]:
    """(ok, message) — ViGEmBus installed? Checks via the registry, WITHOUT
    plugging in a virtual gamepad (non-intrusive check to gray out the button on load)."""
    try:
        import vgamepad  # noqa: F401
    except Exception as e:
        return False, f"vgamepad non installe: {e}"
    try:
        import winreg
        for path in (
            r"SYSTEM\CurrentControlSet\Services\ViGEmBus",
            r"SYSTEM\CurrentControlSet\Services\ViGEmBus.sys",
        ):
            try:
                winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, path).Close()
                return True, "ok"
            except FileNotFoundError:
                continue
        return False, "ViGEmBus driver requis. Installe: github.com/ViGEm/ViGEmBus/releases"
    except Exception as e:
        return False, f"Verification ViGEmBus impossible: {e}"


def connect_gamepad() -> bool:
    """Plugs in the persistent virtual gamepad. True if OK."""
    try:
        _get_shared_pad()
        return True
    except Exception:
        return False


def make_backend(cfg: dict) -> InputBackend:
    mode = cfg.get("input_backend", "keyboard")
    if mode == "gamepad":
        return GamepadBackend(steer_key=cfg.get("steer_key"))
    return KeyboardBackend(
        cfg.get("accelerate_key", "w"), cfg.get("steer_key"),
        cfg.get("reverse_key", "s"),
        cfg.get("steer_left_key", "a"), cfg.get("steer_right_key", "d"),
        rewind_key=cfg.get("rewind_key", "r"),
    )
