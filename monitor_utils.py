from __future__ import annotations

import ctypes
from dataclasses import dataclass
import sys


MONITOR_DEFAULTTONEAREST = 0x00000002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040


@dataclass(frozen=True)
class WorkArea:
    left: int
    top: int
    right: int
    bottom: int


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _Rect(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _MonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint),
        ("rcMonitor", _Rect),
        ("rcWork", _Rect),
        ("dwFlags", ctypes.c_uint),
    ]


def get_monitor_work_area(pointer_x: int, pointer_y: int) -> WorkArea | None:
    """Return the Windows work area containing a virtual-screen point."""
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.windll.user32
        monitor_from_point = user32.MonitorFromPoint
        monitor_from_point.argtypes = (_Point, ctypes.c_uint)
        monitor_from_point.restype = ctypes.c_void_p
        monitor = monitor_from_point(
            _Point(int(pointer_x), int(pointer_y)),
            MONITOR_DEFAULTTONEAREST,
        )
        if not monitor:
            return None

        get_monitor_info = user32.GetMonitorInfoW
        get_monitor_info.argtypes = (ctypes.c_void_p, ctypes.POINTER(_MonitorInfo))
        get_monitor_info.restype = ctypes.c_bool
        info = _MonitorInfo(cbSize=ctypes.sizeof(_MonitorInfo))
        if not get_monitor_info(monitor, ctypes.byref(info)):
            return None
        return WorkArea(
            left=int(info.rcWork.left),
            top=int(info.rcWork.top),
            right=int(info.rcWork.right),
            bottom=int(info.rcWork.bottom),
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def calculate_popup_position(
    pointer_x: int,
    pointer_y: int,
    popup_width: int,
    popup_height: int,
    work_area: WorkArea,
    *,
    offset_x: int = 14,
    offset_y: int = 18,
    padding: int = 8,
) -> tuple[int, int]:
    """Place a popup by the pointer and clamp it to that monitor's work area."""
    minimum_x = work_area.left + padding
    minimum_y = work_area.top + padding
    maximum_x = max(minimum_x, work_area.right - popup_width - padding)
    maximum_y = max(minimum_y, work_area.bottom - popup_height - padding)
    x = min(max(int(pointer_x) + offset_x, minimum_x), maximum_x)
    y = min(max(int(pointer_y) + offset_y, minimum_y), maximum_y)
    return x, y


def position_popup_window(
    window,
    x: int,
    y: int,
    width: int,
    height: int,
    *,
    tk_screen_width: int,
    tk_screen_height: int,
) -> None:
    """Move a Tk top-level to absolute virtual-screen coordinates."""
    if _set_windows_position(window, x, y, width, height):
        return

    # Tk geometry uses a negative sign as a right/bottom anchor. Convert an
    # absolute negative coordinate back to the equivalent offset.
    horizontal = (
        f"+{x}"
        if x >= 0
        else f"-{max(0, int(tk_screen_width) - width - x)}"
    )
    vertical = (
        f"+{y}"
        if y >= 0
        else f"-{max(0, int(tk_screen_height) - height - y)}"
    )
    window.geometry(f"{width}x{height}{horizontal}{vertical}")


def _set_windows_position(window, x: int, y: int, width: int, height: int) -> bool:
    if sys.platform != "win32":
        return False
    try:
        frame_id = window.wm_frame()
        hwnd = int(str(frame_id), 0)
        user32 = ctypes.windll.user32
        set_window_pos = user32.SetWindowPos
        set_window_pos.argtypes = (
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        )
        set_window_pos.restype = ctypes.c_bool
        return bool(
            set_window_pos(
                ctypes.c_void_p(hwnd),
                None,
                int(x),
                int(y),
                int(width),
                int(height),
                SWP_NOZORDER | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
        )
    except (AttributeError, OSError, TypeError, ValueError):
        return False
