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

    def get(self, path: str) -> tuple[int, Any]:
        status, _, raw = self.request("GET", path)
        return status, json.loads(raw or b"{}")

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


def test_a_refused_post_never_corrupts_the_next_request(panel) -> None:  # type: ignore[no-untyped-def]
    """A stale tab (old login cookie) POSTs '{}': the refusal must not leave the body in the
    keep-alive connection, where it became the start of the next request ("{}POST" -> 501)."""
    import socket

    _app, client = panel
    port = client.server.port
    host = f"127.0.0.1:{port}"
    refused = (f"POST /api/remote-dbs/x/delete HTTP/1.1\r\nHost: {host}\r\nCookie: varsha_panel=stale\r\n"
               f"X-Varsha: 1\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{{}}")
    follow_up = f"GET /api/state HTTP/1.1\r\nHost: {host}\r\nCookie: varsha_panel=stale\r\n\r\n"
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall((refused + follow_up).encode())
        data = b""
        while chunk := sock.recv(65536):
            data += chunk
    assert b"403" in data.split(b"\r\n", 1)[0] and b"login has expired" in data
    assert b"501" not in data and b"Unsupported method" not in data


def test_settings_batch_two_apply_at_once(panel, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json
    app, client = panel
    client.login()
    status, data = client.get("/api/settings")
    assert status == 200 and data["quit_action"] == "ask" and data["open_panel_on_start"] is True
    assert client.post("/api/settings/general", {"quit_action": "leave"})[0] == 200
    assert client.get("/api/state")[1]["quit_action"] == "leave"  # the Quit button follows it at once
    assert client.post("/api/settings/general", {"quit_action": "boom"})[0] == 400
    assert client.post("/api/settings/general", {"open_panel_on_start": False})[0] == 200
    assert client.post("/api/settings/notify", {"kinds": ["failures"]})[0] == 200
    assert app.settings.notify == ("failures",)
    folder = tmp_path / "my-backups"
    assert client.post("/api/settings/backup-dir", {"folder": str(folder)})[0] == 200
    assert app.databases.backup_root == folder and folder.is_dir()  # manager.settings changed too
    saved = json.loads(app.paths.settings_file.read_text(encoding="utf-8"))
    assert saved["quit_action"] == "leave" and saved["open_panel_on_start"] is False
    assert saved["tray_notifications"] == {"failures": True, "php_errors": False, "mail": False}
    assert saved["backup_dir"] == str(folder)
    assert client.post("/api/settings/timezone", {"timezone": "not a zone!"})[0] == 400
    assert client.post("/api/settings/timezone", {"timezone": "Europe/London"})[0] == 200
    assert app.engine.settings.php_timezone == "Europe/London"  # the next php.ini uses it
    assert app.settings.panel_port == Settings().panel_port  # never touched by these saves
    status, body = client.post("/api/settings/notify-test", {})
    assert status in (400, 500) or "tray" in str(body)  # no tray in tests


def test_changing_the_tld_renames_everything_that_names_sites(panel, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import json
    app, client = panel
    client.login()
    queued = []
    monkeypatch.setattr(app, "run_task", lambda title, fn, *a, **k: queued.append(title) or 1)  # no admin prompt in tests
    app.php_errors.set_site_ignored("banks.test", True)
    status, preview = client.post("/api/settings/tld-preview", {"tld": ".Lan"})
    assert status == 200 and (preview["old"], preview["new"]) == ("test", "lan")
    assert preview["ignored_sites"] == ["banks.test"]
    assert client.post("/api/settings/tld", {"tld": "dev"})[0] == 400  # .dev forces HTTPS: refused
    assert client.post("/api/settings/tld", {"tld": "no spaces"})[0] == 400
    status, body = client.post("/api/settings/tld", {"tld": "lan"})
    assert status == 200 and "admin rights" in body["message"]
    assert queued == ["Move sites to .lan"]  # vhosts, DNS, hosts and the DNS route follow in a task
    assert app.settings.tld == app.engine.settings.tld == app.manager.settings.tld == app.tunnels.tld == "lan"
    assert app.php_errors.settings["ignored_sites"] == ["banks.lan"]
    assert json.loads(app.paths.settings_file.read_text(encoding="utf-8"))["tld"] == "lan"
    assert client.get("/api/state")[1]["tld"] == "lan"
    assert "already" in client.post("/api/settings/tld", {"tld": "lan"})[1]["message"]


def test_log_size_setting(panel) -> None:  # type: ignore[no-untyped-def]
    import json
    app, client = panel
    client.login()
    assert client.post("/api/settings/log-size", {"mb": 0})[0] == 400
    assert client.post("/api/settings/log-size", {"mb": "lots"})[0] == 400
    status, body = client.post("/api/settings/log-size", {"mb": 5})
    assert status == 200 and "5 MB" in body["message"]
    assert app.settings.log_max_bytes == app.manager.settings.log_max_bytes == 5 * 1024 * 1024
    assert json.loads(app.paths.settings_file.read_text(encoding="utf-8"))["log_max_mb"] == 5
    assert client.get("/api/settings")[1]["log_max_mb"] == 5
    assert client.post("/api/settings/trim-logs", {})[0] == 200


def test_settings_export_and_import_through_the_panel(panel, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import json
    app, client = panel
    client.login()
    app.paths.settings_file.parent.mkdir(parents=True, exist_ok=True)
    app.paths.settings_file.write_text(json.dumps({"quit_action": "stop"}), encoding="utf-8")
    status, headers, body = client.request("GET", "/api/settings/export")
    assert status == 200 and headers["Content-Type"] == "application/zip" and "attachment" in headers["Content-Disposition"]
    zip_path = tmp_path / "export.zip"
    zip_path.write_bytes(body)
    app.paths.settings_file.write_text(json.dumps({"quit_action": "leave"}), encoding="utf-8")
    app.folder_picker = lambda *a: str(zip_path)
    status, data = client.post("/api/settings/import-pick", {})
    staged = data["import"]
    assert status == 200 and {"name": "settings.json", "status": "changed"} in staged["files"]
    assert client.post("/api/settings/import", {"token": "nope"})[0] == 400
    status, data = client.post("/api/settings/import", {"token": staged["token"]})
    assert status == 200 and "Restart varsha" in data["message"]  # no restart hook in tests
    assert json.loads(app.paths.settings_file.read_text(encoding="utf-8"))["quit_action"] == "stop"
    assert list((app.paths.data / "settings-backups").iterdir())
