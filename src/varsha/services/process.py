"""Low-level Windows process helpers: spawn with log capture, stop, kill trees."""

from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path

import psutil

logger = logging.getLogger(__name__)

# Every child process gets this flag: no console window pops up for httpd/mysqld/mailpit.
CREATE_NO_WINDOW: int = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# NTSTATUS codes that show up as exit codes when a Windows binary can't even start.
_EXIT_HINTS: dict[int, str] = {
    0xC0000135: "a required DLL is missing, usually the Microsoft Visual C++ Redistributable "
                "(a 32-bit build needs the x86 one)",
    0xC000007B: "the binary or one of its DLLs has the wrong architecture (32-bit vs 64-bit)",
    0xC0000005: "access violation (the program crashed)",
    0xC0000142: "DLL initialisation failed",
}


def describe_exit_code(code: int | None) -> str:
    if code is None:
        return "unknown exit code"
    unsigned = code & 0xFFFFFFFF
    hint = _EXIT_HINTS.get(unsigned)
    if hint:
        return f"exit code 0x{unsigned:08X}: {hint}"
    if unsigned > 0xFFFF:
        return f"exit code 0x{unsigned:08X}"
    return f"exit code {code}"


def rotate_log(log_path: Path, max_bytes: int) -> bool:
    """Keep one previous log as <name>.log.1 once the log grows past max_bytes. Returns True if
    it rolled over.

    A log a running process holds open can't be renamed on Windows, so then it is copied to .1
    and emptied in place ("copytruncate"): the services append to it, so they carry on writing
    at the new end. Lines written in the instant between the copy and the truncate are lost."""
    try:
        if log_path.stat().st_size <= max_bytes:
            return False
    except OSError:
        return False
    old = log_path.with_name(log_path.name + ".1")
    try:
        log_path.replace(old)
        return True
    except OSError:
        pass
    try:
        with open(log_path, "r+b") as src:
            with open(old, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            src.seek(0)
            src.truncate(0)
        return True
    except OSError as exc:
        logger.warning("Could not trim %s: %s", log_path, exc)
        return False


APP_LOGS = ("varsha.log",)  # varsha's own log rotates itself (logging's RotatingFileHandler)


def trim_logs(logs_dir: Path, max_bytes: int) -> list[str]:
    """Roll over every *.log in the logs folder that is bigger than max_bytes; returns their names."""
    trimmed = []
    try:
        candidates = sorted(logs_dir.glob("*.log"))
    except OSError:
        return trimmed
    for path in candidates:
        if path.name.lower() in APP_LOGS or not path.is_file():
            continue
        if rotate_log(path, max_bytes):
            trimmed.append(path.name)
    if trimmed:
        logger.info("Logs over %d MB rolled over: %s", max_bytes // (1024 * 1024), ", ".join(trimmed))
    return trimmed


def logs_usage(logs_dir: Path) -> dict[str, object]:
    """Total size of the logs folder and its biggest files, for the Settings page."""
    files = []
    try:
        for path in logs_dir.iterdir():
            if path.is_file():
                files.append((path.name, path.stat().st_size))
    except OSError:
        pass
    files.sort(key=lambda f: -f[1])
    return {"total": sum(size for _n, size in files), "count": len(files),
            "biggest": [{"name": n, "size": s} for n, s in files[:5]]}


def spawn(cmd: Sequence[str], cwd: Path, log_path: Path) -> subprocess.Popen[bytes]:
    """Start `cmd` with stdout and stderr appended to `log_path`.

    The child inherits the log file handle and writes to it directly, so logging keeps
    working even while the GUI is busy, and after varsha exits if services are left running.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.write(f"\n===== {stamp} varsha starting: {subprocess.list2cmdline(cmd)}\n".encode())
        log.flush()
        return subprocess.Popen(
            list(cmd),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW,
            close_fds=True,
        )


def launch_tool(exe: Path, args: Sequence[str] = ()) -> None:
    """Start a GUI tool (HeidiSQL, ...) that varsha doesn't track; it outlives varsha."""
    subprocess.Popen(
        [str(exe), *args],
        cwd=str(exe.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
        close_fds=True,
    )


def run_quiet(cmd: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Run a short helper command (e.g. mysqladmin shutdown) with no window and captured output."""
    return subprocess.run(
        list(cmd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        creationflags=CREATE_NO_WINDOW,
    )


def is_alive(proc: psutil.Process | None) -> bool:
    # is_running() also compares the creation time, so a reused PID doesn't count as alive.
    if proc is None:
        return False
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def kill_processes(procs: Iterable[psutil.Process], timeout: float = 5.0) -> None:
    alive: list[psutil.Process] = []
    for proc in procs:
        try:
            proc.kill()
            alive.append(proc)
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            logger.warning("Access denied killing PID %s", proc.pid)
    psutil.wait_procs(alive, timeout=timeout)


def kill_tree(proc: psutil.Process, timeout: float = 5.0) -> None:
    """Kill a process and all its children.

    Apache on Windows runs a parent httpd.exe and a child httpd.exe that holds the ports;
    killing only the parent leaves the child running with port 80 still bound.
    """
    try:
        children = proc.children(recursive=True)
    except psutil.Error:
        children = []
    kill_processes([*children, proc], timeout=timeout)


def signal_apache_restart(pid: int) -> bool:
    """Graceful restart of a console Apache: the same mechanism as shutdown, with the
    "ap<pid>_restart" event. The parent re-reads httpd.conf and replaces its child;
    in-flight requests finish first."""
    return _set_apache_event(pid, "restart")


def signal_apache_shutdown(pid: int) -> bool:
    """Ask a console-mode Apache to shut down gracefully.

    `httpd -k stop` only reaches Apache when it's installed as a Windows service. A console
    httpd (how varsha runs it) listens on a named event "ap<parent pid>_shutdown" instead;
    mpm_winnt creates it at startup, and setting it is what Ctrl+C would do.
    Returns False if the event doesn't exist (not Apache, or already gone).
    """
    return _set_apache_event(pid, "shutdown")


def _set_apache_event(pid: int, what: str) -> bool:
    if sys.platform != "win32":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenEventW.restype = ctypes.c_void_p
    kernel32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    kernel32.SetEvent.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    event_modify_state = 0x0002
    handle = kernel32.OpenEventW(event_modify_state, False, f"ap{pid}_{what}")
    if not handle:
        logger.info("Apache %s event for PID %s not found (error %s)", what, pid, ctypes.get_last_error())
        return False
    try:
        return bool(kernel32.SetEvent(handle))
    finally:
        kernel32.CloseHandle(handle)


def same_path(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))
