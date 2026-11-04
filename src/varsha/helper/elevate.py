"""Run megha elevated (UAC prompt) from the unprivileged GUI and read its result."""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from varsha.errors import ServiceError
from varsha.paths import AppPaths

logger = logging.getLogger(__name__)

SEE_MASK_NOCLOSEPROCESS = 0x00000040
SEE_MASK_NOASYNC = 0x00000100
SW_HIDE = 0
ERROR_CANCELLED = 1223
INFINITE = 0xFFFFFFFF


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", ctypes.wintypes.HWND),
        ("lpVerb", ctypes.c_wchar_p), ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
        ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int), ("hInstApp", ctypes.wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p), ("lpClass", ctypes.c_wchar_p), ("hkeyClass", ctypes.wintypes.HKEY),
        ("dwHotKey", ctypes.wintypes.DWORD), ("hIconOrMonitor", ctypes.wintypes.HANDLE),
        ("hProcess", ctypes.wintypes.HANDLE),
    ]


class ElevationCancelled(ServiceError):
    pass


def megha_command(paths: AppPaths) -> list[str]:
    """Varsha.exe --run-module when frozen; in a source checkout, pythonw -m the module."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable)), "--run-module", "varsha.helper.megha"]
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return [str(pythonw if pythonw.is_file() else Path(sys.executable)), "-m", "varsha.helper.megha"]


RESULT_WAIT = 60.0  # seconds to wait for megha's result when Windows gave us no process to wait on
RESULT_GRACE = 5.0  # seconds to wait for the result file after megha's process ended


def _read_result(result_file: Path) -> dict[str, object] | None:
    try:
        outcome = json.loads(result_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return outcome if isinstance(outcome, dict) and "ok" in outcome else None


def _wait_for_result(result_file: Path, timeout: float) -> dict[str, object] | None:
    deadline = time.monotonic() + timeout
    while True:
        outcome = _read_result(result_file)
        if outcome is not None or time.monotonic() >= deadline:
            return outcome
        time.sleep(0.2)


def run_elevated(paths: AppPaths, args: Sequence[str], request: dict[str, object] | None = None,
                 verify: Callable[[], str | None] | None = None) -> str:
    """Run megha with admin rights. Blocks until it is done; call from a worker thread.

    Some Windows setups (e.g. Windows 11's "Administrator protection") hand back no process to
    wait on, or let the call return before megha has written its result. So the result file is
    polled for a while, and when it never appears, `verify()` checks the effect itself (it needs
    no admin rights): a message means the change is in place.

    Raises ElevationCancelled if the user says No at the UAC prompt.
    """
    work = Path(tempfile.mkdtemp(prefix="varsha-megha-"))  # in the user's private %TEMP%
    try:
        result_file = work / "result.json"
        full = [*args, "--result", str(result_file)]
        if request is not None:
            request_file = work / "request.json"
            request_file.write_text(json.dumps(request), encoding="utf-8")
            full += ["--request", str(request_file)]
        command = megha_command(paths)
        code, waited = _shell_execute_runas(command[0], [*command[1:], *full], cwd=paths.root)
        outcome = _wait_for_result(result_file, RESULT_GRACE if waited else RESULT_WAIT)
        if outcome is None:
            logger.warning("megha (%s) gave no result: exit code %s, process handle %s, files %s",
                           args[0], code, "yes" if waited else "none",
                           sorted(p.name for p in work.iterdir()))
            confirmed = verify() if verify else None
            if confirmed:
                return confirmed
            raise ServiceError(f"megha exited ({code}) without a result. Check {paths.app_log.name} for details.")
        if not outcome.get("ok"):
            raise ServiceError(f"megha: {outcome.get('message', 'failed')}")
        return str(outcome.get("message", "done"))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _shell_execute_runas(exe: str, args: Sequence[str], cwd: Path) -> tuple[int | None, bool]:
    """Start `exe` elevated. Returns (exit code, whether we could wait for the process)."""
    if sys.platform != "win32":  # pragma: no cover
        raise ServiceError("Elevation is only supported on Windows")
    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS | SEE_MASK_NOASYNC
    info.lpVerb = "runas"  # triggers the UAC prompt
    info.lpFile = exe
    info.lpParameters = subprocess.list2cmdline(list(args))
    info.lpDirectory = str(cwd)
    info.nShow = SW_HIDE
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
    kernel32.WaitForSingleObject.argtypes = [ctypes.wintypes.HANDLE, ctypes.wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [ctypes.wintypes.HANDLE, ctypes.POINTER(ctypes.wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        error = ctypes.get_last_error()
        if error == ERROR_CANCELLED:
            raise ElevationCancelled("You declined the administrator prompt, so nothing was changed.")
        raise ServiceError(f"Could not start megha (error {error})")
    if not info.hProcess:
        return None, False  # Windows started it but gave us nothing to wait on
    try:
        kernel32.WaitForSingleObject(info.hProcess, INFINITE)
        code = ctypes.wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code)):
            return None, False
        return int(code.value), True
    finally:
        kernel32.CloseHandle(info.hProcess)


def dns_route_active(tld: str) -> bool:
    """Whether the NRPT rule exists (reading the table needs no admin rights)."""
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-DnsClientNrptRule | Where-Object {{ $_.Comment -eq 'varsha' -and $_.Namespace -contains '.{tld}' }}"
             " | Measure-Object).Count"],
            capture_output=True, text=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return result.stdout.strip().isdigit() and int(result.stdout.strip()) > 0
    except (OSError, subprocess.SubprocessError):
        return False
