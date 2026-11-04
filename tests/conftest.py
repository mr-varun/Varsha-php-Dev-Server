from __future__ import annotations

import socket
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from varsha.errors import ServiceError
from varsha.paths import AppPaths
from varsha.services.manager import ServiceManager
from varsha.services.models import Installation, ServiceDef, StopMethod, StopSpec
from varsha.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_SERVICE = Path(__file__).with_name("fake_service.py")
# In a venv, sys.executable is a launcher that re-spawns the real python.exe as a child,
# so its PID would not be the process that owns the port. Use the base interpreter.
PYTHON = Path(getattr(sys, "_base_executable", sys.executable))
STOP_CODE = (
    "import socket,sys; socket.create_connection(('127.0.0.1', int(sys.argv[1]))).sendall(b'stop')"
)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(condition: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def fake_def(sid: str, port: int, *extra_args: str, stop: StopSpec | None = None) -> ServiceDef:
    return ServiceDef(
        id=sid,
        name=f"Fake {sid}",
        type="generic",
        exe="bin/fake/python.exe",  # not used: tests inject a locator
        args=(str(FAKE_SERVICE), "--port", "{port}", *extra_args),
        ports=(port,),
        stop=stop or StopSpec(StopMethod.COMMAND, (str(PYTHON), "-c", STOP_CODE, "{port}")),
    )


@pytest.fixture
def app_paths(tmp_path: Path) -> AppPaths:
    paths = AppPaths(tmp_path)
    paths.ensure_dirs()
    return paths


@pytest.fixture
def make_manager(app_paths: AppPaths) -> Iterator[Callable[..., ServiceManager]]:
    managers: list[ServiceManager] = []

    def factory(services: list[ServiceDef], **overrides: object) -> ServiceManager:
        options: dict[str, object] = dict(
            start_timeout=10.0, stop_timeout=5.0, monitor_interval=0.1, restart_delay=0.1,
            stable_after=600.0,
        )
        options.update(overrides)
        manager = ServiceManager(
            app_paths, services, Settings(**options),  # type: ignore[arg-type]
            locator=lambda _defn: Installation(PYTHON, "3.12"),
        )
        managers.append(manager)
        return manager

    yield factory
    for manager in managers:
        manager.shutdown()
        try:
            manager.stop_all()
        except ServiceError:
            pass
