"""Plain data types for services. The manager owns the mutable runtime state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class ServiceState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    ERROR = "error"
    MISSING = "missing"  # binary not found under bin\
    DISABLED = "disabled"  # "enabled": false in services.json

    @property
    def label(self) -> str:
        return {"missing": "Not installed"}.get(self.value, self.value.capitalize())


class StopMethod(StrEnum):
    COMMAND = "command"  # run stop.command (e.g. mysqladmin shutdown)
    APACHE_SIGNAL = "apache_signal"  # set Apache's named shutdown event
    TERMINATE = "terminate"  # TerminateProcess


@dataclass(frozen=True)
class StopSpec:
    method: StopMethod = StopMethod.TERMINATE
    command: tuple[str, ...] = ()
    # Child process names to kill right after the graceful request (e.g. Apache's php-cgi.exe
    # workers, which mod_fcgid otherwise waits ~10 s for before killing them itself).
    also_kill: tuple[str, ...] = ()


BUILTIN_TYPE = "builtin"


@dataclass(frozen=True)
class Link:
    """A quick link shown in a service's menu (e.g. Mailpit's inbox). `url` may use placeholders."""

    label: str
    url: str


@dataclass(frozen=True)
class ReloadSpec:
    """How to apply config changes without a full stop/start."""

    method: str = "restart"  # "restart" (stop + start) or "apache_signal" (graceful restart)
    test_command: tuple[str, ...] = ()  # run first; a non-zero exit cancels the reload


@dataclass(frozen=True)
class ServiceDef:
    """One entry of config/services.json. Paths are relative to the varsha root."""

    id: str
    name: str
    type: str
    exe: str | None = None  # glob pattern, e.g. "bin/apache/*/bin/httpd.exe"
    args: tuple[str, ...] = ()
    ports: tuple[int, ...] = ()
    config_template: str | None = None
    config_file: str | None = None
    data_dir: str | None = None
    init_command: tuple[str, ...] = ()
    init_stdin: tuple[str, ...] = ()  # "sql:..." literal SQL, anything else a file to feed
    log_file: str | None = None  # the service's own log (e.g. Apache ErrorLog), shown by "Logs"
    tools: dict[str, str] = field(default_factory=dict)  # "client", "dump" for databases
    password_reset_sql: tuple[str, ...] = ()  # statements for --init-file, with {password}
    links: tuple[Link, ...] = ()
    module: str | None = None  # built-in services: the Python module varsha runs
    reload: ReloadSpec = field(default_factory=ReloadSpec)
    autostart: bool = False
    enabled: bool = True
    stop: StopSpec = field(default_factory=StopSpec)

    @property
    def is_builtin(self) -> bool:
        return self.type == BUILTIN_TYPE

    @property
    def is_database(self) -> bool:
        """MySQL-compatible: has a command-line client, so backup/restore/phpMyAdmin apply."""
        return "client" in self.tools


@dataclass(frozen=True)
class Installation:
    """A binary found on disk for a service."""

    exe: Path
    version: str | None


@dataclass(frozen=True)
class ServiceStatus:
    """Immutable snapshot handed to the GUI."""

    id: str
    name: str
    type: str
    state: ServiceState
    version: str | None
    ports: tuple[int, ...]
    pid: int | None
    message: str
    log_path: Path  # what "Logs" shows: log_file if set, else the captured console output
    autostart: bool
    is_database: bool = False
