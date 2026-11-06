"""The tray's small "Import Banks Data" window: the panel page in a browser app window, kept on top.

Edge (or Chrome) opens the page with --app=..., which gives a window without tabs or an address
bar. Browsers have no switch for "always on top", so varsha finds the window by its title (the
page sets it) and makes it topmost through Windows itself, sized and placed in the screen's
bottom-right corner. If that window is already open it is brought to the front instead.
"""

from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger(__name__)

MINI_TITLE = "Import Banks Data - varsha mini"  # what bankimport's page sets as document.title
SIZE = (540, 560)  # in 96-dpi pixels
HWND_TOPMOST = -1
SWP_SHOWWINDOW = 0x0040
SPI_GETWORKAREA = 0x0030
SW_RESTORE = 9


def find_browser() -> Path | None:
    roots = [os.environ.get(k, "") for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    names = [r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"]
    for name in names:
        for root in roots:
            if root and (Path(root) / name).is_file():
                return Path(root) / name
    return None


def _windows_with(title: str) -> list[int]:
    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def visit(hwnd: int, _lparam: int) -> bool:
        if user32.IsWindowVisible(hwnd):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if title in buffer.value:
                    found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    return found


def _user32() -> ctypes.WinDLL:
    """user32 with exact argument types: HWND_TOPMOST is the handle -1, and passed as a plain int
    on 64-bit Windows it would arrive as 0xFFFFFFFF, a different (and meaningless) handle."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SetWindowPos.restype = wintypes.BOOL
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
    user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]
    return user32


def _pin(hwnd: int) -> None:
    """Topmost, sized and placed bottom-right of the work area (the screen minus the taskbar)."""
    user32 = _user32()
    try:
        scale = user32.GetDpiForWindow(hwnd) / 96.0 or 1.0
    except (AttributeError, OSError):
        scale = 1.0
    width, height = int(SIZE[0] * scale), int(SIZE[1] * scale)
    area = wintypes.RECT()
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(area), 0)
    margin = int(16 * scale)
    x, y = area.right - width - margin, area.bottom - height - margin
    user32.ShowWindow(hwnd, SW_RESTORE)
    if not user32.SetWindowPos(hwnd, wintypes.HWND(HWND_TOPMOST), x, y, width, height, SWP_SHOWWINDOW):
        logger.warning("Couldn't keep the Import Banks Data window on top (error %s)", ctypes.get_last_error())
    user32.SetForegroundWindow(hwnd)


def _pin_when_it_appears(timeout: float = 25.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        windows = _windows_with(MINI_TITLE)
        if windows:
            time.sleep(0.3)  # let the browser finish its own placement first
            for hwnd in windows:
                _pin(hwnd)
            return
        time.sleep(0.25)
    logger.warning("The Import Banks Data window didn't appear within %.0f s", timeout)


def open_bank_window(login_url: str) -> bool:
    """Open (or bring back) the mini window. False when no Edge / Chrome is installed."""
    existing = _windows_with(MINI_TITLE)
    if existing:
        for hwnd in existing:
            _pin(hwnd)
        return True
    browser = find_browser()
    if browser is None:
        return False
    url = login_url + "#bankmini"
    subprocess.Popen([str(browser), f"--app={url}", f"--window-size={SIZE[0]},{SIZE[1]}"],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)
    threading.Thread(target=_pin_when_it_appears, name="bank-window", daemon=True).start()
    return True
