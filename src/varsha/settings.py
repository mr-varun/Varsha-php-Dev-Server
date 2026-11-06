"""Load config/settings.json into a typed `Settings` object."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from varsha.errors import ConfigError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExtraDbServer:
    """A MySQL server varsha doesn't run but phpMyAdmin should list (e.g. Laragon's on 3306)."""

    name: str
    host: str = "127.0.0.1"
    port: int = 3306


# None by default: MySQL 5.6 now uses 3306 itself. Add e.g. another local MySQL here.
DEFAULT_EXTRA_SERVERS: tuple[ExtraDbServer, ...] = ()

# What the panel's Quit button does: ask every time, stop the services, or leave them running.
QUIT_ACTIONS = ("ask", "stop", "leave")
# Tray balloons, per kind (settings.json "tray_notifications": {"mail": false, ...}).
NOTIFY_KINDS = {
    "failures": "Something failed (a service, a backup, a task)",
    "php_errors": "New PHP errors",
    "mail": "New mail in Mailpit",
}


@dataclass(frozen=True)
class Settings:
    ask_before_exit: bool = True
    start_timeout: float = 20.0
    stop_timeout: float = 15.0
    max_auto_restarts: int = 3
    log_max_bytes: int = 10 * 1024 * 1024
    default_php: str = "8.3"  # version prefix; Apache and phpMyAdmin use the newest match
    php_timezone: str = "UTC"
    phpmyadmin_extra_servers: tuple[ExtraDbServer, ...] = DEFAULT_EXTRA_SERVERS
    tld: str = "test"  # sites are <folder>.<tld>; never "dev" (a real TLD that forces HTTPS)
    panel_port: int = 7700  # the browser control panel, on 127.0.0.1 only
    # Where database backups go (one subfolder per server). Empty: <root>/data/backups.
    # The one setting that may be absolute, e.g. "D:\\Varsha-Backups", so backups survive a reinstall.
    backup_dir: str = ""
    open_panel_on_start: bool = True  # a normal start opens the panel in the browser (--startup never does)
    quit_action: str = "ask"  # one of QUIT_ACTIONS
    notify: tuple[str, ...] = tuple(NOTIFY_KINDS)  # the kinds of tray balloons that are shown
    # Not in settings.json (yet): tuning knobs, overridden by tests.
    stable_after: float = 60.0  # a service running this long gets its crash counter reset
    restart_delay: float = 1.0
    monitor_interval: float = 1.0


def _positive_number(key: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f'settings.json: "{key}" must be a positive number, got {value!r}')
    return float(value)


def load_settings(path: Path) -> Settings:
    """Read settings.json. A missing file means defaults; unknown keys are kept for later phases."""
    if not path.is_file():
        logger.info("No %s, using default settings", path.name)
        return Settings()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"{path.name} is not valid JSON (line {exc.lineno}, column {exc.colno}): {exc.msg}"
        ) from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path.name}: expected a JSON object")

    settings = Settings()
    if "ask_before_exit" in raw:
        if not isinstance(raw["ask_before_exit"], bool):
            raise ConfigError('settings.json: "ask_before_exit" must be true or false')
        settings = replace(settings, ask_before_exit=raw["ask_before_exit"])
    if "start_timeout_seconds" in raw:
        settings = replace(settings, start_timeout=_positive_number("start_timeout_seconds", raw["start_timeout_seconds"]))
    if "stop_timeout_seconds" in raw:
        settings = replace(settings, stop_timeout=_positive_number("stop_timeout_seconds", raw["stop_timeout_seconds"]))
    if "max_auto_restarts" in raw:
        value = raw["max_auto_restarts"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfigError('settings.json: "max_auto_restarts" must be a whole number >= 0')
        settings = replace(settings, max_auto_restarts=value)
    if "log_max_mb" in raw:
        mb = _positive_number("log_max_mb", raw["log_max_mb"])
        settings = replace(settings, log_max_bytes=int(mb * 1024 * 1024))
    for key in ("default_php", "php_timezone"):
        if key in raw:
            if not isinstance(raw[key], str) or not raw[key].strip():
                raise ConfigError(f'settings.json: "{key}" must be a non-empty string')
            settings = replace(settings, **{key: raw[key].strip()})
    if "tld" in raw:
        from varsha.sites.discovery import validate_tld  # local import: avoids an import cycle

        if not isinstance(raw["tld"], str):
            raise ConfigError('settings.json: "tld" must be a string like "test"')
        settings = replace(settings, tld=validate_tld(raw["tld"]))
    if "panel_port" in raw:
        port = raw["panel_port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ConfigError('settings.json: "panel_port" must be a number from 1024 to 65535')
        settings = replace(settings, panel_port=port)
    if "backup_dir" in raw:
        value = raw["backup_dir"]
        if not isinstance(value, str):
            raise ConfigError('settings.json: "backup_dir" must be a folder path (text), e.g. "D:\\\\Varsha-Backups"')
        value = value.strip()
        if value and ".." in Path(value).parts:
            raise ConfigError('settings.json: "backup_dir" must not contain ".."')
        settings = replace(settings, backup_dir=value)
    if "phpmyadmin_extra_servers" in raw:
        settings = replace(settings, phpmyadmin_extra_servers=_extra_servers(raw["phpmyadmin_extra_servers"]))
    if "open_panel_on_start" in raw:
        if not isinstance(raw["open_panel_on_start"], bool):
            raise ConfigError('settings.json: "open_panel_on_start" must be true or false')
        settings = replace(settings, open_panel_on_start=raw["open_panel_on_start"])
    if "quit_action" in raw:
        if raw["quit_action"] not in QUIT_ACTIONS:
            raise ConfigError(f'settings.json: "quit_action" must be one of {", ".join(QUIT_ACTIONS)}')
        settings = replace(settings, quit_action=raw["quit_action"])
    if "tray_notifications" in raw:
        value = raw["tray_notifications"]
        if not isinstance(value, dict) or not all(isinstance(v, bool) for v in value.values()):
            raise ConfigError('settings.json: "tray_notifications" must be like {"mail": false}')
        settings = replace(settings, notify=tuple(k for k in NOTIFY_KINDS if value.get(k, True)))
    return settings


def _extra_servers(value: Any) -> tuple[ExtraDbServer, ...]:
    where = 'settings.json: "phpmyadmin_extra_servers"'
    if not isinstance(value, list):
        raise ConfigError(f"{where} must be a list")
    servers = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise ConfigError(f'{where}: each entry needs a "name" (and optional "host", "port")')
        port = item.get("port", 3306)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ConfigError(f"{where}: invalid port {port!r}")
        host = item.get("host", "127.0.0.1")
        if not isinstance(host, str) or not host.strip():
            raise ConfigError(f'{where}: "host" must be a string')
        servers.append(ExtraDbServer(item["name"], host.strip(), port))
    return tuple(servers)
