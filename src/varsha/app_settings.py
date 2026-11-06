"""The Settings page: start with Windows, auto-start services, the control panel's port.

Start with Windows uses the current user's Run key (HKCU\\...\\CurrentVersion\\Run), so no admin
rights are needed and nothing is installed. The deployed Varsha.exe and a source checkout get their
own value names, so they never overwrite each other's entry. The entry passes --startup, which
starts varsha quietly in the tray.

Auto-start is the "autostart" flag of each service in config/services.json; it's written into that
file (other keys and the comment keys are kept) and applied to the running manager at once.
"""

from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

from varsha.errors import ConfigError
from varsha.paths import AppPaths

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_FLAG = "--startup"
PORT_MIN, PORT_MAX = 1024, 65535


# ---------------------------------------------------------------------- start with Windows

def startup_name() -> str:
    return "varsha" if getattr(sys, "frozen", False) else "varsha (dev)"


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        parts = [sys.executable, STARTUP_FLAG]
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        parts = [str(pythonw if pythonw.is_file() else Path(sys.executable)), "-m", "varsha", STARTUP_FLAG]
    return " ".join(f'"{p}"' if " " in p or p.endswith(".exe") else p for p in parts)


class StartupEntry:
    """The Run-key value for this copy of varsha. `key` is replaceable for tests."""

    def __init__(self, key: str = RUN_KEY, name: str | None = None) -> None:
        self.key = key
        self.name = name or startup_name()

    def _open(self, write: bool = False):  # noqa: ANN202
        import winreg
        access = winreg.KEY_READ | (winreg.KEY_SET_VALUE if write else 0)
        if write:
            return winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, self.key, 0, access)
        return winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.key, 0, access)

    def current(self) -> str | None:
        import winreg
        try:
            with self._open() as key:
                value, _kind = winreg.QueryValueEx(key, self.name)
                return str(value)
        except OSError:
            return None

    def status(self) -> dict[str, Any]:
        value = self.current()
        wanted = startup_command()
        return {"enabled": value is not None, "matches": value == wanted, "command": value or wanted, "name": self.name}

    def set(self, enabled: bool) -> str:
        import winreg
        if enabled:
            with self._open(write=True) as key:
                winreg.SetValueEx(key, self.name, 0, winreg.REG_SZ, startup_command())
            return "varsha starts with Windows (quietly, in the tray)"
        try:
            with self._open(write=True) as key:
                winreg.DeleteValue(key, self.name)
        except FileNotFoundError:
            pass
        return "varsha no longer starts with Windows"


# ---------------------------------------------------------------------- auto-start services

def read_services_file(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Can't read {path.name}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("services"), list):
        raise ConfigError(f"{path.name} has no services list")
    return raw


def set_autostart_flags(path: Path, wanted: set[str]) -> list[str]:
    """Write autostart true for the services in `wanted`, false for the others. Returns the ids changed."""
    raw = read_services_file(path)
    known = {s.get("id") for s in raw["services"] if isinstance(s, dict)}
    unknown = sorted(wanted - known)
    if unknown:
        raise ConfigError(f"Unknown services: {', '.join(unknown)}")
    changed = []
    for entry in raw["services"]:
        if not isinstance(entry, dict):
            continue
        flag = entry.get("id") in wanted
        if bool(entry.get("autostart", False)) != flag:
            entry["autostart"] = flag
            changed.append(entry["id"])
    if changed:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    return changed


# ---------------------------------------------------------------------- panel port

def check_panel_port(port: Any, current: int, service_ports: set[int]) -> int:
    """A port the control panel can move to (raises ValueError with the reason)."""
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("The port must be a number") from None
    if not PORT_MIN <= port <= PORT_MAX:
        raise ValueError(f"Choose a port from {PORT_MIN} to {PORT_MAX}")
    if port == current:
        return port
    if port in service_ports:
        raise ValueError(f"Port {port} belongs to one of varsha's services")
    with socket.socket() as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            raise ValueError(f"Port {port} is in use by another program") from None
    return port


def settings_snapshot(paths: AppPaths, panel_port: int, running_port: int, manager: Any,
                      started_with: int | None = None) -> dict[str, Any]:
    services = []
    for status in manager.statuses():
        defn = manager.definition(status.id)
        services.append({"id": defn.id, "name": defn.name, "autostart": bool(defn.autostart),
                         "enabled": bool(getattr(defn, "enabled", True)), "ports": list(defn.ports)})
    return {"startup": StartupEntry().status(), "services": services,
            "panel_port": panel_port, "running_port": running_port,
            # the saved port differs from the one this session was started with (a busy port at start
            # makes varsha take the next free one: that alone needs no restart)
            "restart_needed": panel_port != (started_with if started_with is not None else running_port),
            "fallback": started_with is not None and running_port != started_with}
