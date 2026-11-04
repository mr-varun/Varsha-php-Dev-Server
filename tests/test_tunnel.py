from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from pathlib import Path

import psutil
import pytest

from conftest import PYTHON, free_port, wait_for
from varsha.errors import ConfigError
from varsha.paths import AppPaths
from varsha.tunnel.manager import TunnelError, TunnelManager, TunnelState
from varsha.tunnel.target import parse_target

FAKE = Path(__file__).with_name("fake_cloudflared.py")


# ---------------------------------------------------------------------- target parsing

@pytest.mark.parametrize(
    ("text", "origin", "host_header", "shares_localhost"),
    [
        ("localhost:8025", "http://127.0.0.1:8025", "localhost:8025", True),
        ("http://127.0.0.1", "http://127.0.0.1:80", "localhost", True),
        ("myapp.test", "http://127.0.0.1:80", "myapp.test", False),
        ("https://shop.test/admin", "https://127.0.0.1:443", "shop.test", False),
        ("http://api.myapp.test:8080", "http://127.0.0.1:8080", "api.myapp.test:8080", False),
    ],
)
def test_parse_target(text: str, origin: str, host_header: str, shares_localhost: bool) -> None:
    target = parse_target(text)
    assert target.origin == origin
    assert target.host_header == host_header
    assert target.shares_localhost is shares_localhost


@pytest.mark.parametrize("text", ["", "example.com", "http://192.168.1.10", "ftp://localhost",
                                  "http://localhost:99999"])
def test_rejects_non_local_or_invalid(text: str) -> None:
    with pytest.raises(ConfigError):
        parse_target(text)


# ---------------------------------------------------------------------- manager

@pytest.fixture
def origin() -> Iterator[int]:
    """A listening socket standing in for the local web server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        yield server.getsockname()[1]


@pytest.fixture
def make_tunnels(app_paths: AppPaths) -> Iterator[object]:
    created: list[TunnelManager] = []

    def factory(mode: str = "ok", timeout: float = 10.0,
                dns_check: Callable[[str], bool] = lambda _host: True,
                dns_timeout: float = 10.0) -> TunnelManager:
        manager = TunnelManager(app_paths, command=[str(PYTHON), str(FAKE), "--mode", mode],
                                start_timeout=timeout, dns_check=dns_check, dns_timeout=dns_timeout)
        created.append(manager)
        return manager

    yield factory
    for manager in created:
        manager.stop_all()


def test_start_returns_public_url_and_stop(make_tunnels, origin: int) -> None:  # type: ignore[no-untyped-def]
    tunnels: TunnelManager = make_tunnels()
    events: list[int] = []
    tunnels.set_listener(lambda: events.append(1))
    status = tunnels.start(f"localhost:{origin}")
    assert status.state is TunnelState.RUNNING
    assert status.public_url == "https://quiet-river-demo-test.trycloudflare.com"
    assert tunnels.any_running() and events

    log = tunnels.log_path.read_text(encoding="utf-8")
    assert f"origin=http://127.0.0.1:{origin}" in log
    assert f"host=localhost:{origin}" in log
    assert "--no-autoupdate" in log

    pid = status.pid
    tunnels.stop(status.id)
    assert tunnels.statuses() == []
    assert wait_for(lambda: not psutil.pid_exists(pid), 5)


def test_same_target_is_shared_once(make_tunnels, origin: int) -> None:  # type: ignore[no-untyped-def]
    tunnels: TunnelManager = make_tunnels()
    first = tunnels.start(f"localhost:{origin}")
    second = tunnels.start(f"http://127.0.0.1:{origin}")
    assert first.id == second.id and len(tunnels.statuses()) == 1


def test_nothing_listening(make_tunnels) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(TunnelError, match="Nothing is listening"):
        make_tunnels().start(f"localhost:{free_port()}")


def test_cloudflared_error_is_reported(make_tunnels, origin: int) -> None:  # type: ignore[no-untyped-def]
    tunnels: TunnelManager = make_tunnels("fail")
    with pytest.raises(TunnelError, match="no network"):
        tunnels.start(f"localhost:{origin}")
    assert tunnels.statuses() == []


def test_timeout_kills_cloudflared(make_tunnels, origin: int) -> None:  # type: ignore[no-untyped-def]
    tunnels: TunnelManager = make_tunnels("silent", timeout=1.5)
    with pytest.raises(TunnelError, match="no public link"):
        tunnels.start(f"localhost:{origin}")
    assert not tunnels.any_running() and tunnels.statuses() == []


def test_link_is_not_shown_before_registration(make_tunnels, origin: int) -> None:  # type: ignore[no-untyped-def]
    tunnels: TunnelManager = make_tunnels("url-only", timeout=1.5)
    with pytest.raises(TunnelError, match="no public link"):
        tunnels.start(f"localhost:{origin}")


def test_link_waits_for_public_dns(make_tunnels, origin: int) -> None:  # type: ignore[no-untyped-def]
    asked: list[str] = []

    def dns(host: str) -> bool:
        asked.append(host)
        return len(asked) >= 2  # "appears" on the second lookup

    tunnels: TunnelManager = make_tunnels(dns_check=dns)
    status = tunnels.start(f"localhost:{origin}")
    assert asked == ["quiet-river-demo-test.trycloudflare.com"] * 2
    assert status.state is TunnelState.RUNNING and status.message == ""


def test_link_still_shown_if_dns_never_confirms(make_tunnels, origin: int) -> None:  # type: ignore[no-untyped-def]
    tunnels: TunnelManager = make_tunnels(dns_check=lambda _h: False, dns_timeout=0.5)
    status = tunnels.start(f"localhost:{origin}")
    assert status.state is TunnelState.RUNNING and status.public_url
    assert "flushdns" in status.message


def test_unexpected_exit_marks_error(make_tunnels, origin: int) -> None:  # type: ignore[no-untyped-def]
    tunnels: TunnelManager = make_tunnels("die-later")
    status = tunnels.start(f"localhost:{origin}")
    assert wait_for(lambda: tunnels.statuses()[0].state is TunnelState.ERROR, 5)
    assert "no longer works" in tunnels.statuses()[0].message
    tunnels.stop(status.id)  # "Dismiss"
    assert tunnels.statuses() == []


def test_not_installed(app_paths: AppPaths) -> None:
    tunnels = TunnelManager(app_paths)  # tmp root has no bin\cloudflared
    assert not tunnels.installed
    with pytest.raises(TunnelError, match="not installed"):
        tunnels.start("localhost:8025")
