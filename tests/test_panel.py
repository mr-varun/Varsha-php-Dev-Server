"""The browser control panel: security model, state, and a few actions."""

from __future__ import annotations

import http.client
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from conftest import fake_def, free_port, wait_for
from varsha.config_engine.engine import ConfigEngine
from varsha.paths import AppPaths
from varsha.secrets import CredentialStore
from varsha.services.manager import ServiceManager
from varsha.services.remote import RemoteDbStore
from varsha.settings import Settings
from varsha.sites.manager import SiteManager
from varsha.tunnel.manager import TunnelManager
from varsha.web.panel import PanelApp
from varsha.web.server import COOKIE, PanelServer, bind_panel


class Client:
    def __init__(self, server: PanelServer) -> None:
        self.server = server
        self.cookie = ""

    def request(self, method: str, path: str, body: Any = None, headers: dict[str, str] | None = None,
                host: str | None = None) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        all_headers = {"Host": host or f"127.0.0.1:{self.server.port}"}
        if self.cookie:
            all_headers["Cookie"] = self.cookie
        all_headers.update(headers or {})
        data = json.dumps(body).encode() if body is not None else None
        conn.request(method, path, body=data, headers=all_headers)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()

    def login(self) -> None:
        status, headers, _ = self.request("GET", f"/?token={self.server.token}")
        assert status == 303
        self.cookie = headers["Set-Cookie"].split(";")[0]

    def post(self, path: str, body: Any = None) -> tuple[int, Any]:
        status, _, raw = self.request("POST", path, body or {}, {"X-Varsha": "1", "Content-Type": "application/json"})
        return status, json.loads(raw or b"{}")


@pytest.fixture
def panel(app_paths: AppPaths, make_manager) -> Iterator[tuple[PanelApp, Client]]:  # type: ignore[no-untyped-def]
    manager: ServiceManager = make_manager([fake_def("svc", free_port())])
    settings = Settings()
    creds = CredentialStore(app_paths.data / "credentials.json")
    remote = RemoteDbStore(app_paths.config / "remote-databases.json", creds)
    engine = ConfigEngine(app_paths, settings, [manager.definition("svc")], remote=remote)
    app = PanelApp(app_paths, settings, manager, engine, SiteManager(engine, manager), TunnelManager(app_paths), remote=remote)
    app._dns_route_cache = (float("inf"), False)  # noqa: SLF001 - skip the PowerShell lookup in tests
    server = bind_panel(app, free_port())
    server.start()
    yield app, Client(server)
    server.shutdown()
    app.shutdown()


def test_requires_token_cookie(panel) -> None:  # type: ignore[no-untyped-def]
    _app, client = panel
    assert client.request("GET", "/api/state")[0] == 401
    assert client.request("GET", "/?token=wrong")[0] == 401
    client.login()
    assert f"{COOKIE}=" in client.cookie
    status, _, body = client.request("GET", "/api/state")
    assert status == 200 and json.loads(body)["services"][0]["id"] == "svc"


def test_rejects_rebinding_and_cross_site_posts(panel) -> None:  # type: ignore[no-untyped-def]
    _app, client = panel
    client.login()
    assert client.request("GET", "/api/state", host="evil.example")[0] == 421
    assert client.request("POST", "/api/rescan", {})[0] == 403  # no X-Varsha header
    status, _, _ = client.request("POST", "/api/rescan", {}, {"X-Varsha": "1", "Origin": "http://evil.example"})
    assert status == 403
    assert client.post("/api/rescan")[0] == 202


def test_security_headers(panel) -> None:  # type: ignore[no-untyped-def]
    _app, client = panel
    client.login()
    _status, headers, _ = client.request("GET", "/")
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert headers["X-Frame-Options"] == "DENY"


def test_start_stop_through_api(panel) -> None:  # type: ignore[no-untyped-def]
    app, client = panel
    client.login()
    assert client.post("/api/services/svc/start")[0] == 202
    assert wait_for(lambda: app.manager.status("svc").state.value == "running", 10)
    assert client.post("/api/services/svc/stop")[0] == 202
    assert wait_for(lambda: app.manager.status("svc").state.value == "stopped", 10)
    assert client.post("/api/services/svc/explode")[0] == 400


def test_open_folder_takes_keywords_not_paths(panel) -> None:  # type: ignore[no-untyped-def]
    _app, client = panel
    client.login()
    status, body = client.post("/api/open", {"what": "C:/Windows"})
    assert status == 409 and "Unknown folder" in body["error"]


def test_backup_names_cannot_escape(panel) -> None:  # type: ignore[no-untyped-def]
    app, _client = panel
    from varsha.errors import ServiceError
    with pytest.raises(ServiceError):
        app.backup_file("svc", "../../config/settings.json")


def test_remote_db_crud(panel) -> None:  # type: ignore[no-untyped-def]
    app, client = panel
    client.login()
    status, body = client.post("/api/remote-dbs", {"name": "Prod", "host": "db.example.com", "user": "admin",
                                                   "password": "secret", "remember": True, "ssl": True})
    assert status == 200 and body["id"] == "prod"
    state = json.loads(client.request("GET", "/api/state")[2])
    (remote,) = state["remote_dbs"]
    assert remote["has_password"] is True and "password" not in remote
    assert client.post("/api/remote-dbs", {"name": "Bad", "host": "not a host!", "user": "x"})[0] == 409
    assert client.post("/api/remote-dbs/prod/delete")[0] == 200
    assert json.loads(client.request("GET", "/api/state")[2])["remote_dbs"] == []
    assert app.remote.credentials.get("remote:prod") == ""  # type: ignore[union-attr]


def test_panel_port_cannot_be_shared(panel) -> None:  # type: ignore[no-untyped-def]
    app, _client = panel
    from varsha.tunnel.manager import TunnelError
    app.tunnels.blocked_ports = {8777}
    app.tunnels._command = [str(Path(__file__))]  # noqa: SLF001 - "installed" without a real cloudflared
    with pytest.raises(TunnelError, match="control panel"):
        app.tunnels.start("localhost:8777")
