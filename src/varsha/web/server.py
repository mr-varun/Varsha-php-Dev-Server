"""The control panel's HTTP server (stdlib only), bound to 127.0.0.1.

Security model, since anything on this port can start/stop services and touch databases:
- Loopback only, and the Host header must be 127.0.0.1:<port> or localhost:<port>. That
  stops DNS-rebinding pages (evil.example resolving to 127.0.0.1) from talking to us.
- A random token per varsha run. The URL varsha opens carries it once (?token=...); the
  server swaps it for an HttpOnly, SameSite=Strict cookie and redirects to a clean URL.
- Every state-changing request is a POST with the header X-Varsha: 1. Browsers can't send
  custom headers cross-origin without a CORS preflight, which this server never approves,
  so other websites can't forge actions even though the cookie is ours.
- Strict Content-Security-Policy; the page loads nothing from outside.
"""

from __future__ import annotations

import hmac
import json
import logging
import re
import secrets
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from varsha.errors import VarshaError
from varsha.web.panel import PanelApp

logger = logging.getLogger(__name__)

STATIC = Path(__file__).with_name("static")
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/logo.png": ("logo.png", "image/png"),
    "/favicon.ico": ("varsha.ico", "image/x-icon"),
}
COOKIE = "varsha_panel"
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
MAX_UPLOAD = 4 * 1024 * 1024 * 1024
SERVICE_ACTIONS = {"start", "stop", "restart", "backup", "password", "heidisql", "restore"}


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False  # on Windows this would let another process steal the port

    def __init__(self, app: PanelApp, port: int) -> None:
        self.app = app
        self.token = secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), PanelHandler)
        self.port = self.server_address[1]
        self.allowed_hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def login_url(self) -> str:
        return f"{self.url}?token={self.token}"

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, name="panel-http", daemon=True)
        thread.start()
        return thread


def bind_panel(app: PanelApp, preferred: int, tries: int = 20) -> PanelServer:
    """Bind the preferred port, or the next free one."""
    last: OSError | None = None
    for port in range(preferred, preferred + tries):
        try:
            return PanelServer(app, port)
        except OSError as exc:
            last = exc
    raise OSError(f"No free port for the control panel near {preferred}: {last}")


class PanelHandler(BaseHTTPRequestHandler):
    server: PanelServer
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ plumbing

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        logger.debug("panel %s - %s", self.address_string(), format % args)

    @property
    def app(self) -> PanelApp:
        return self.server.app

    def _headers(self, status: int, content_type: str, length: int | None, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message})

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "").lower() in self.server.allowed_hosts

    def _authed(self) -> bool:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE and hmac.compare_digest(value, self.server.token):
                return True
        return False

    def _body_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 1024 * 1024:
            raise ValueError("request too large")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw or b"{}")
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:  # noqa: N802 - stdlib name
        if not self._host_ok():
            self._error(HTTPStatus.MISDIRECTED_REQUEST, "wrong Host")
            return
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        token = (query.get("token") or [""])[0]
        if token and hmac.compare_digest(token, self.server.token):
            # Swap the one-time URL token for a cookie and drop it from the address bar.
            self._headers(HTTPStatus.SEE_OTHER, "text/plain", 0, {
                "Location": "/",
                "Set-Cookie": f"{COOKIE}={self.server.token}; HttpOnly; SameSite=Strict; Path=/",
            })
            return
        if parts.path in ("/favicon.ico", "/static/logo.png"):
            self._static(parts.path)  # harmless and needed before login
            return
        if not self._authed():
            self._locked_page()
            return
        if parts.path in STATIC_FILES:
            self._static(parts.path)
        elif parts.path == "/api/state":
            self._json(HTTPStatus.OK, self.app.state())
        elif parts.path == "/api/events":
            self._events()
        elif m := re.fullmatch(r"/api/logs/([a-z0-9._-]+)", parts.path):
            offset = int((query.get("offset") or ["-1"])[0])
            self._guarded(lambda: self._json(HTTPStatus.OK, self.app.read_log(m.group(1), offset)))
        elif m := re.fullmatch(r"/api/services/([a-z0-9._-]+)/backups", parts.path):
            self._guarded(lambda: self._json(HTTPStatus.OK, self.app.backups(m.group(1))))
        elif m := re.fullmatch(r"/api/php/([A-Za-z0-9._-]+)", parts.path):
            self._guarded(lambda: self._json(HTTPStatus.OK, self.app.php_details(m.group(1))))
        else:
            self._error(HTTPStatus.NOT_FOUND, "not found")

    def _static(self, path: str) -> None:
        name, content_type = STATIC_FILES[path]
        body = (STATIC / name).read_bytes()
        self._headers(HTTPStatus.OK, content_type, len(body))
        self.wfile.write(body)

    def _locked_page(self) -> None:
        body = (b"<!doctype html><meta charset=utf-8><title>varsha</title>"
                b"<body style='font:16px Bahnschrift,Segoe UI,sans-serif;background:#f5f1e8;padding:48px'>"
                b"<h1>varsha control panel</h1><p>Open it from the varsha tray icon "
                b"(right-click &rarr; Open control panel). The link from the tray logs you in.</p>")
        self._headers(HTTPStatus.UNAUTHORIZED, "text/html; charset=utf-8", len(body))
        self.wfile.write(body)

    def _events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        bus = self.app.events
        last = bus.last
        try:
            self.wfile.write(b"event: hello\ndata: {}\n\n")
            self.wfile.flush()
            while not bus.closed:
                events = bus.wait(last, timeout=15)
                if not events:
                    self.wfile.write(b": ping\n\n")  # keeps the connection (and proxies) alive
                for seq, kind, data in events:
                    last = seq
                    self.wfile.write(f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
                self.wfile.flush()
                if events and all(kind == "state" for _s, kind, _d in events):
                    time.sleep(0.1)  # coalesce bursts of state changes
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, socket.timeout):
            pass

    # ------------------------------------------------------------------ POST

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._error(HTTPStatus.MISDIRECTED_REQUEST, "wrong Host")
            return
        if not self._authed() or self.headers.get("X-Varsha") != "1":
            self._error(HTTPStatus.FORBIDDEN, "not allowed")
            return
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() not in self.server.allowed_hosts:
            self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
            return
        path = urlsplit(self.path).path
        if m := re.fullmatch(r"/api/services/([a-z0-9._-]+)/restore-upload", path):
            self._guarded(lambda: self._restore_upload(m.group(1)))
            return
        try:
            body = self._body_json()
        except (ValueError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
            return
        self._guarded(lambda: self._post(path, body))

    def _guarded(self, action: Any) -> None:
        try:
            action()
        except VarshaError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except (KeyError, ValueError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))

    def _post(self, path: str, body: dict[str, Any]) -> None:
        app = self.app
        mgr = app.manager

        def task(title: str, fn: Any, *args: Any, success: Any = None, sid: str = "") -> None:
            task_id = app.run_task(title, fn, *args, success=success, service_id=sid)
            self._json(HTTPStatus.ACCEPTED, {"task": task_id})

        if m := re.fullmatch(r"/api/services/([a-z0-9._-]+)/([a-z-]+)", path):
            sid, action = m.group(1), m.group(2)
            if action not in SERVICE_ACTIONS:
                raise KeyError(f"unknown action {action}")
            name = mgr.definition(sid).name
            if action in ("start", "stop", "restart"):
                task(f"{action.capitalize()} {name}", getattr(mgr, action), sid, sid=sid)
            elif action == "backup":
                task(f"Back up {name}", app.databases.backup, sid,
                     success=lambda p: f"Saved {p.name}", sid=sid)
            elif action == "restore":
                file = app.backup_file(sid, str(body.get("file", "")))
                task(f"Restore {name}", app.databases.restore, sid, file,
                     success=lambda _r: f"{file.name} restored", sid=sid)
            elif action == "password":
                password = body.get("password", "")
                if not isinstance(password, str):
                    raise ValueError("password must be text")
                task(f"Set root password of {name}", mgr.reset_root_password, sid, password,
                     success=lambda _r: "Password set" if password else "Password removed", sid=sid)
            elif action == "heidisql":
                app.open_heidisql(sid)
                self._json(HTTPStatus.OK, {"ok": True})
        elif path in ("/api/all/start", "/api/all/stop"):
            verb = path.rsplit("/", 1)[1]
            task(f"{verb.capitalize()} all", mgr.start_all if verb == "start" else mgr.stop_all)
        elif path == "/api/sites/sync":
            task("Rescan sites", app.sites.sync, success=lambda changed: "Sites updated" if changed else "Already up to date")
        elif path == "/api/hosts/update":
            task("Update hosts file", app.sites.update_hosts, success=lambda msg: msg)
        elif path == "/api/dns/route":
            enable = bool(body.get("enable"))

            def route() -> str:
                message = app.sites.dns_route(enable)
                app.dns_route_active(refresh=True)
                return message

            task("Route .{} to varsha DNS".format(app.settings.tld) if enable else "Remove DNS route", route,
                 success=lambda msg: msg)
        elif path == "/api/https/trust":
            task("Trust local HTTPS", app.trust_https, success=lambda msg: msg)
        elif path == "/api/rescan":
            task("Rescan bin", mgr.refresh, success=lambda _r: "Binaries rescanned")
        elif path == "/api/tunnels":
            target = str(body.get("target", ""))
            task(f"Share {target}", app.tunnels.start, target,
                 success=lambda status: f"Public link: {status.public_url}")
        elif m := re.fullmatch(r"/api/tunnels/(t\d+)/stop", path):
            task("Stop sharing", app.tunnels.stop, m.group(1))
        elif path == "/api/open":
            app.open_folder(str(body.get("what", "")), str(body.get("ref", "")))
            self._json(HTTPStatus.OK, {"ok": True})
        elif path == "/api/remote-dbs":
            self._json(HTTPStatus.OK, {"id": app.remote_save(body, None)})
        elif path == "/api/remote-dbs/test":
            db_id = str(body.get("id") or "") or None
            task(f"Test {body.get('name') or 'connection'}", app.remote_test, body, db_id, success=lambda msg: msg)
        elif m := re.fullmatch(r"/api/remote-dbs/([a-z0-9-]+)", path):
            self._json(HTTPStatus.OK, {"id": app.remote_save(body, m.group(1))})
        elif m := re.fullmatch(r"/api/remote-dbs/([a-z0-9-]+)/delete", path):
            app.remote_delete(m.group(1))
            self._json(HTTPStatus.OK, {"ok": True})
        elif m := re.fullmatch(r"/api/php/([A-Za-z0-9._-]+)/default", path):
            task("Switch default PHP", app.tools.set_default_php, m.group(1), success=lambda msg: msg)
        elif m := re.fullmatch(r"/api/php/([A-Za-z0-9._-]+)", path):
            extensions, ini = body.get("extensions", {}), body.get("ini", {})
            task(f"Save PHP settings", app.tools.save_php_config, m.group(1), extensions, ini, success=lambda msg: msg)
        elif m := re.fullmatch(r"/api/sites/([a-z0-9.-]+)/php", path):
            version = str(body.get("php") or "") or None

            def switch_php(domain: str = m.group(1)) -> str:
                message = app.tools.set_site_php(domain, version)
                app.sites.sync()
                return message

            task(f"PHP for {m.group(1)}", switch_php, success=lambda msg: msg)
        elif m := re.fullmatch(r"/api/sites/([a-z0-9.-]+)/delete", path):
            if body.get("confirm") != m.group(1):
                raise ValueError("type the site name to confirm")

            def delete(domain: str = m.group(1)) -> str:
                message = app.tools.delete_site(domain)
                app.sites.sync()
                return message

            task(f"Delete {m.group(1)}", delete, success=lambda msg: msg)
        elif m := re.fullmatch(r"/api/sites/([a-z0-9.-]+)/terminal", path):
            self._json(HTTPStatus.OK, {"message": app.tools.open_terminal(m.group(1))})
        elif path == "/api/terminal":
            self._json(HTTPStatus.OK, {"message": app.tools.open_terminal(None)})
        elif path == "/api/create":
            kind, name = str(body.get("kind", "")), str(body.get("name", "")).strip().lower()
            db = str(body.get("db") or "") or None
            php = str(body.get("php") or "") or None

            def create() -> str:
                message = app.tools.create(kind, name, db, php)
                app.sites.sync()
                return message

            task(f"Create {name}.{app.settings.tld}", create, success=lambda msg: msg)
        elif path == "/api/quit":
            self._json(HTTPStatus.OK, {"ok": True})
            threading.Thread(target=app.quit, args=(bool(body.get("stop_services")),), daemon=True).start()
        else:
            self._error(HTTPStatus.NOT_FOUND, "not found")

    def _restore_upload(self, sid: str) -> None:
        """Stream an uploaded .sql into data/backups/<id>/ and restore it."""
        app = self.app
        defn = app.manager.definition(sid)
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_UPLOAD:
            raise ValueError("empty or too large upload")
        raw_name = self.headers.get("X-Filename", "upload.sql")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(raw_name).name)[:80] or "upload.sql"
        if not safe.lower().endswith(".sql"):
            safe += ".sql"
        folder = app.databases.backup_dir(defn.id)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"uploaded-{time.strftime('%Y%m%d-%H%M%S')}-{safe}"
        remaining = length
        with open(target, "wb") as out:
            while remaining:
                chunk = self.rfile.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
        if remaining:
            target.unlink(missing_ok=True)
            raise ValueError("upload interrupted")
        task_id = app.run_task(f"Restore {defn.name}", app.databases.restore, sid, target,
                               success=lambda _r: f"{raw_name} restored", service_id=sid)
        self._json(HTTPStatus.ACCEPTED, {"task": task_id})
