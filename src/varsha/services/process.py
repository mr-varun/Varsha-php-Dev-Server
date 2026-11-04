"""Low-level Windows process helpers: spawn with log capture, stop, kill trees."""

from __future__ import annotations

import ctypes
import logging
import os
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


def rotate_log(log_path: Path, max_bytes: int) -> None:
    """Keep one previous log as <name>.log.1 once the log grows past max_bytes."""
    try:
        if log_path.stat().st_size <= max_bytes:
            return
        log_path.replace(log_path.with_name(log_path.name + ".1"))
    except FileNotFoundError:
        return
    except OSError as exc:
        # Fails if a process still holds the file open (Windows doesn't allow renaming then).
        logger.warning("Could not rotate %s: %s", log_path, exc)


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
