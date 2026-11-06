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
    "/static/data.js": ("data.js", "text/javascript; charset=utf-8"),
    "/static/rescue.js": ("rescue.js", "text/javascript; charset=utf-8"),
    "/static/mysqlrescue.js": ("mysqlrescue.js", "text/javascript; charset=utf-8"),
    "/static/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
    "/static/logo.png": ("logo.png", "image/png"),
    "/favicon.ico": ("varsha.ico", "image/x-icon"),
}
COOKIE = "varsha_panel"
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
# An email's HTML is untrusted: no scripts, no forms, no network except (on request) remote images,
# and a sandbox on top. Inline images arrive as data: URIs, so the sandboxed (opaque-origin) frame
# never needs the panel cookie. It may only be framed by the panel itself.
MAIL_CSP = ("default-src 'none'; img-src data:{remote}; style-src 'unsafe-inline'{remote}; font-src data:{remote}; "
            "frame-ancestors 'self'; form-action 'none'; sandbox allow-popups allow-popups-to-escape-sandbox")
MAX_UPLOAD = 4 * 1024 * 1024 * 1024
REFUSE_DRAIN_LIMIT = 64 * 1024  # after refusing a POST, read at most this much before closing
REFUSE_LINGER_SECONDS = 2
SERVICE_ACTIONS = {"start", "stop", "restart", "reload", "backup", "password", "heidisql", "restore",
                   "autobackup", "autobackup-run"}


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

    def _headers(self, status: int, content_type: str, length: int | None, extra: dict[str, str] | None = None,
                 csp: str = CSP, frame_options: str = "DENY") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", frame_options)
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
        elif m := re.fullmatch(r"/api/services/([a-z0-9._-]+)/databases", parts.path):
            self._guarded(lambda: self._json(HTTPStatus.OK, self.app.databases.list_databases(m.group(1))))
        elif m := re.fullmatch(r"/api/php/([A-Za-z0-9._-]+)", parts.path):
            self._guarded(lambda: self._json(HTTPStatus.OK, self.app.php_details(m.group(1))))
        elif parts.path == "/api/settings/export":
            self._guarded(lambda: self._download(*self._export_settings()))
        elif parts.path == "/api/settings":
            self._guarded(lambda: self._json(HTTPStatus.OK, self.app.settings_data()))
        elif m := re.fullmatch(r"/api/(rescue|mysql-rescue)", parts.path):
            self._guarded(lambda: self._json(HTTPStatus.OK, self.app.rescue_listing(m.group(1))))
        elif m := re.fullmatch(r"/api/(rescue|mysql-rescue)/job/(job-[\w.-]{1,120})", parts.path):
            self._guarded(lambda: self._json(HTTPStatus.OK, self.app.rescuer(m.group(1)).job(m.group(2))))
        elif parts.path == "/api/data":
            self._guarded(lambda: self._json(HTTPStatus.OK, self.app.data.listing()))
        elif m := re.fullmatch(r"/api/data/([0-9a-f]{16})(?:/(rows|structure|cell|export\.sql|export\.csv))?", parts.path):
            self._guarded(lambda: self._data_get(m.group(1), m.group(2) or "", query))
        elif m := re.fullmatch(r"/api/lan/([a-z0-9.-]+)/qr", parts.path):
            self._guarded(lambda: self._json(HTTPStatus.OK, self.app.sites.lan_qr(m.group(1))))
        elif parts.path == "/api/sites/git":
            refresh = (query.get("refresh") or [""])[0] == "1"
            self._guarded(lambda: self._json(HTTPStatus.OK, self.app.site_git(refresh)))
        elif parts.path == "/api/php-errors":
            arg = lambda name: (query.get(name) or [""])[0]  # noqa: E731
            self._guarded(lambda: self._json(HTTPStatus.OK, self.app.php_errors.groups(arg("severity"), arg("site"), arg("q"))))
        elif parts.path == "/api/import/suggest":
            self._guarded(lambda: self._json(HTTPStatus.OK, self.app.importer.suggestions()))
        elif m := re.fullmatch(r"/api/import/([0-9a-f]{12})", parts.path):
            self._guarded(lambda: self._json(HTTPStatus.OK, self.app.importer.session(m.group(1)).public()))
        elif parts.path.startswith("/api/mail/"):
            self._guarded(lambda: self._mail_get(parts.path, query))
        else:
            self._error(HTTPStatus.NOT_FOUND, "not found")

    def _mail_get(self, path: str, query: dict[str, list[str]]) -> None:
        mail = self.app.mail
        arg = lambda name, default="": (query.get(name) or [default])[0]  # noqa: E731
        if path == "/api/mail/messages":
            self._json(HTTPStatus.OK, mail.messages(int(arg("start", "0") or 0), int(arg("limit", "50") or 50), arg("q")))
        elif m := re.fullmatch(r"/api/mail/message/([A-Za-z0-9_-]+)", path):
            data = mail.message(m.group(1))
            self.app.mail_watch.poll()  # opening marks it read: update the badge right away
            self._json(HTTPStatus.OK, data)
        elif m := re.fullmatch(r"/api/mail/message/([A-Za-z0-9_-]+)/html", path):
            remote = " https: http:" if arg("remote") == "1" else ""
            body = mail.html(m.group(1)).encode("utf-8")
            self._headers(HTTPStatus.OK, "text/html; charset=utf-8", len(body),
                          csp=MAIL_CSP.format(remote=remote), frame_options="SAMEORIGIN")
            self.wfile.write(body)
        elif m := re.fullmatch(r"/api/mail/message/([A-Za-z0-9_-]+)/headers", path):
            self._json(HTTPStatus.OK, mail.headers(m.group(1)))
        elif m := re.fullmatch(r"/api/mail/message/([A-Za-z0-9_-]+)/html-check", path):
            self._json(HTTPStatus.OK, mail.html_check(m.group(1)))
        elif m := re.fullmatch(r"/api/mail/message/([A-Za-z0-9_-]+)/link-check", path):
            self._json(HTTPStatus.OK, mail.link_check(m.group(1), follow=arg("follow") == "1"))
        elif m := re.fullmatch(r"/api/mail/message/([A-Za-z0-9_-]+)/raw", path):
            body = mail.raw(m.group(1))
            self._download(body, "message/rfc822", f"{m.group(1)}.eml")
        elif m := re.fullmatch(r"/api/mail/message/([A-Za-z0-9_-]+)/part/([0-9.]+)", path):
            body, ctype, name = mail.part(m.group(1), m.group(2))
            self._download(body, ctype, name)
        else:
            self._error(HTTPStatus.NOT_FOUND, "not found")

    def _export_settings(self) -> tuple[bytes, str, str]:
        body, name = self.app.export_settings()
        return body, "application/zip", name

    def _download(self, body: bytes, content_type: str, filename: str) -> None:
        """Always a download (attachment), never rendered in the panel's origin."""
        safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", Path(filename).name).strip(" .")[:120] or "download"
        if not re.fullmatch(r"[a-z0-9.+-]+/[a-z0-9.+-]+", content_type.split(";")[0].strip().lower()):
            content_type = "application/octet-stream"
        self._headers(HTTPStatus.OK, content_type.split(";")[0].strip(), len(body),
                      {"Content-Disposition": f'attachment; filename="{safe}"'}, csp="default-src 'none'; sandbox")
        self.wfile.write(body)

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

    def _refuse(self, status: int, message: str) -> None:
        """Answer a POST we won't process. Its body must not stay in the connection: on a
        keep-alive connection it would be read as the start of the next request ("{}POST ...").

        Closing a socket that still has unread data (the body, or a request pipelined after it)
        makes Windows send a TCP reset, and the client may then get "connection reset" instead of
        this answer. So it's a lingering close, as Apache does: answer, stop sending, then read
        and drop what the client still sends (bounded in size and time) before closing."""
        self.close_connection = True
        self._error(status, message)
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
            self.connection.settimeout(REFUSE_LINGER_SECONDS)
            left = REFUSE_DRAIN_LIMIT
            while left > 0 and (chunk := self.rfile.read1(min(left, 16384))):
                left -= len(chunk)
        except (OSError, socket.timeout, ValueError):
            pass

    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._refuse(HTTPStatus.MISDIRECTED_REQUEST, "wrong Host")
            return
        if not self._authed():
            self._refuse(HTTPStatus.FORBIDDEN, "This page's login has expired (varsha was restarted). "
                                               "Open the control panel again from the varsha tray icon.")
            return
        if self.headers.get("X-Varsha") != "1":
            self._refuse(HTTPStatus.FORBIDDEN, "not allowed")
            return
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc.lower() not in self.server.allowed_hosts:
            self._refuse(HTTPStatus.FORBIDDEN, "cross-origin request refused")
            return
        path = urlsplit(self.path).path
        if m := re.fullmatch(r"/api/services/([a-z0-9._-]+)/restore-upload", path):
            self.close_connection = True  # a refused upload leaves its (large) body unread
            self._guarded(lambda: self._restore_upload(m.group(1)))
            return
        try:
            body = self._body_json()
        except (ValueError, json.JSONDecodeError) as exc:
            self._refuse(HTTPStatus.BAD_REQUEST, str(exc))  # the body may be only partly read
            return
        self._guarded(lambda: self._post(path, body))

    def _data_get(self, file_id: str, what: str, query: dict[str, list[str]]) -> None:
        data = self.app.data
        arg = lambda name, default="": (query.get(name) or [default])[0]  # noqa: E731
        if what == "":
            self._json(HTTPStatus.OK, data.overview(file_id))
        elif what == "structure":
            self._json(HTTPStatus.OK, data.structure(file_id, arg("table")))
        elif what == "rows":
            self._json(HTTPStatus.OK, data.rows(file_id, arg("table"), int(arg("offset", "0")), int(arg("limit", "100")),
                                                arg("order"), arg("desc") == "1", arg("q"), arg("column")))
        elif what == "cell":
            self._json(HTTPStatus.OK, data.get_cell(file_id, arg("table"), json.loads(arg("key", "null")), arg("column")))
        elif what == "export.sql":
            body, name = data.export_sql(file_id)
            self._download(body, "application/sql", name)
        else:
            body, name = data.export_csv(file_id, arg("table"))
            self._download(body, "text/csv", name)

    def _data_post(self, file_id: str, action: str, body: dict[str, Any]) -> Any:
        data = self.app.data
        table = str(body.get("table") or "")
        if action == "mode":
            data.set_edit(file_id, body.get("edit") is True)
            return "Edit mode on: changes are saved to the file (a backup is made first)" if body.get("edit") is True \
                else "Read-only again"
        if action == "update":
            return data.update_cell(file_id, table, body.get("key"), str(body.get("column") or ""), body.get("value"))
        if action == "insert":
            return data.insert_row(file_id, table, body.get("values"))
        if action == "delete":
            return data.delete_rows(file_id, table, body.get("keys"))
        if action == "sql":
            return data.run_sql(file_id, str(body.get("sql") or ""))
        if action == "add-column":
            return data.add_column(file_id, table, str(body.get("name") or ""), str(body.get("type") or ""), body.get("default"))
        if action == "drop-table":
            if body.get("confirm") != table:
                raise ValueError("type the table name to confirm")
            return data.drop_table(file_id, table)
        if action == "backup":
            return data.backup(file_id)
        if action == "restore":
            return data.restore(file_id, str(body.get("name") or ""))
        if action == "integrity":
            return data.integrity(file_id)
        if action == "vacuum":
            return data.vacuum(file_id)
        if action == "forget":
            data.forget(file_id)
            return "Removed from the list (the file itself is untouched)"
        raise ValueError(f"unknown action {action}")

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
            elif action == "reload":  # Apache only: new config, httpd -t, graceful restart (no dropped requests)
                if mgr.definition(sid).type != "apache":
                    raise ValueError("Only Apache has a graceful reload")
                task(f"Reload {name}", mgr.reload, sid, success=lambda r: f"{name} reloaded" if r == "reloaded" or "graceful" in str(r) else f"{name}: {r}", sid=sid)
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
            elif action == "autobackup":
                plan = app.scheduler.set_backup_plan(sid, body)
                app.events.publish("state")
                self._json(HTTPStatus.OK, {"describe": plan.describe()})
            elif action == "autobackup-run":
                app.run_backup_now(sid)
                self._json(HTTPStatus.ACCEPTED, {"ok": True})
        elif path == "/api/all/restart":
            task("Restart running services", mgr.restart_running,
                 success=lambda names: ("Restarted " + ", ".join(names)) if names else "Nothing is running")
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
        elif m := re.fullmatch(r"/api/lan/([a-z0-9.-]+)/(share|stop)", path):
            domain, verb = m.group(1), m.group(2)
            if verb == "share":
                task(f"Share {domain} on the LAN", app.sites.lan_share, domain, success=lambda msg: msg)
            else:
                task(f"Stop LAN sharing {domain}", app.sites.lan_unshare, domain, success=lambda msg: msg)
        elif path == "/api/lan/firewall":
            enable = body.get("enable") is True
            task("Allow LAN sharing in the firewall" if enable else "Remove the LAN firewall rule",
                 app.sites.lan_firewall, enable, success=lambda msg: msg)
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
        elif path == "/api/settings/startup":
            self._json(HTTPStatus.OK, {"message": app.set_startup(body.get("enable") is True)})
        elif path == "/api/settings/autostart":
            self._json(HTTPStatus.OK, {"message": app.set_autostart(body.get("services"))})
        elif path == "/api/settings/panel-port":
            self._json(HTTPStatus.OK, app.set_panel_port(body.get("port")))
        elif path == "/api/settings/general":
            self._json(HTTPStatus.OK, {"message": app.set_general(body)})
        elif path == "/api/settings/timezone":
            self._json(HTTPStatus.OK, {"message": app.set_timezone(body.get("timezone"))})
        elif path == "/api/settings/backup-dir":
            self._json(HTTPStatus.OK, {"message": app.set_backup_dir(body.get("folder"))})
        elif path == "/api/settings/notify":
            self._json(HTTPStatus.OK, {"message": app.set_notify(body.get("kinds"))})
        elif path == "/api/settings/import-pick":
            self._json(HTTPStatus.OK, {"import": app.pick_settings_import()})
        elif path == "/api/settings/import":
            self._json(HTTPStatus.OK, {"message": app.import_settings(body.get("token"))})
        elif path == "/api/settings/log-size":
            self._json(HTTPStatus.OK, {"message": app.set_log_size(body.get("mb"))})
        elif path == "/api/settings/trim-logs":
            self._json(HTTPStatus.OK, {"message": app.trim_logs()})
        elif path == "/api/settings/tld-preview":
            self._json(HTTPStatus.OK, app.tld_preview(body.get("tld")))
        elif path == "/api/settings/tld":
            self._json(HTTPStatus.OK, {"message": app.set_tld(body.get("tld"))})
        elif path == "/api/settings/notify-test":
            self._json(HTTPStatus.OK, {"message": app.notify_test()})
        elif path == "/api/settings/restart":
            self._json(HTTPStatus.OK, {"message": "Restarting: this page reconnects on the new address"})
            threading.Thread(target=app.restart, daemon=True).start()
        elif m := re.fullmatch(r"/api/(rescue|mysql-rescue)/(pick|start|stop|forget)", path):
            kind, action = m.group(1), m.group(2)
            rescuer = app.rescuer(kind)
            if action == "pick":
                self._json(HTTPStatus.OK, {"file": app.pick_rescue_file(kind)})
            elif action == "start":
                options = body.get("options") if isinstance(body.get("options"), dict) else {}
                self._json(HTTPStatus.OK, {"message": rescuer.start(str(body.get("file") or ""), str(body.get("preset") or ""), options)})
            elif action == "stop":
                self._json(HTTPStatus.OK, {"message": rescuer.stop()})
            else:
                rescuer.forget(str(body.get("file") or ""))
                self._json(HTTPStatus.OK, {"ok": True})
        elif m := re.fullmatch(r"/api/(rescue|mysql-rescue)/job/(job-[\w.-]{1,120})/(open|data|import|clean|delete)", path):
            kind, job, action = m.group(1), m.group(2), m.group(3)
            rescuer = app.rescuer(kind)
            if action == "clean":
                self._json(HTTPStatus.OK, {"message": rescuer.clean_job(job)})
            elif action == "delete":
                self._json(HTTPStatus.OK, {"message": rescuer.delete_job(job)})
            elif action == "open":
                self._json(HTTPStatus.OK, {"message": rescuer.open(job, str(body.get("what") or "folder"))})
            elif action == "data":
                self._json(HTTPStatus.OK, {"file": app.rescue_to_data(job, str(body.get("key") or ""))})
            else:
                service, database = str(body.get("service") or ""), str(body.get("database") or "").strip()
                key = str(body.get("key") or "sql_dump")
                task(f"Import into {database}", app.rescue_import, job, service, database, kind, key, success=lambda msg: msg)
        elif path == "/api/data/pick":
            self._json(HTTPStatus.OK, {"file": app.pick_data_file()})
        elif m := re.fullmatch(r"/api/data/([0-9a-f]{16})/([a-z-]+)", path):
            self._json(HTTPStatus.OK, {"message": self._data_post(m.group(1), m.group(2), body)})
        elif path == "/api/pick-folder":
            # A native Windows folder dialog, shown by the tray app: the page gets back the path
            # the user picked, but can never make varsha read or list a folder by itself.
            self._json(HTTPStatus.OK, {"path": app.pick_folder(str(body.get("title") or "Choose a folder"))})
        elif path == "/api/create":
            kind, name = str(body.get("kind", "")), str(body.get("name", "")).strip().lower()
            # a linked project already has its own database settings: never create one for it
            db = None if kind == "link" else (str(body.get("db") or "") or None)
            php = str(body.get("php") or "") or None
            source = str(body.get("source") or "") or None
            if kind == "link":
                app.tools.link_source(source)  # a wrong folder is reported in the dialog, before anything starts

            def create() -> str:
                message = app.tools.create(kind, name, db, php, source=source)
                app.sites.sync()
                return message

            task(f"Create {name}.{app.settings.tld}", create, success=lambda msg: msg)
        elif path in ("/api/php-errors/seen", "/api/php-errors/clear", "/api/php-errors/settings"):
            errors = app.php_errors
            if path.endswith("seen"):
                errors.mark_seen()
            elif path.endswith("clear"):
                errors.clear()
            else:
                errors.set_notify(str(body.get("notify", "")))
            app.events.publish("state")
            self._json(HTTPStatus.OK, {"ok": True})
        elif path == "/api/php-errors/ignore-site":
            message = app.php_errors.set_site_ignored(str(body.get("site") or ""), body.get("ignore") is True)
            app.events.publish("state")
            self._json(HTTPStatus.OK, {"message": message})
        elif m := re.fullmatch(r"/api/php-errors/([0-9a-f]{12})/(dismiss|mute|unmute|open)", path):
            gid, action = m.group(1), m.group(2)
            message = "ok"
            if action == "dismiss":
                app.php_errors.dismiss(gid)
            elif action in ("mute", "unmute"):
                app.php_errors.group(gid)
                app.php_errors.set_muted(gid, action == "mute")
            else:
                message = app.open_php_error(gid)
            app.events.publish("state")
            self._json(HTTPStatus.OK, {"message": message})
        elif path == "/api/doctor/run":
            self._json(HTTPStatus.OK, app.doctor.run())
        elif path == "/api/doctor/fix":
            message = app.doctor.fix(str(body.get("fix", "")))
            app.events.publish("state")
            self._json(HTTPStatus.OK, {"message": message})
        elif path == "/api/import/inspect":
            self._json(HTTPStatus.OK, app.import_inspect(body))
        elif m := re.fullmatch(r"/api/import/([0-9a-f]{12})/run", path):
            app.import_run(m.group(1), body)
            self._json(HTTPStatus.ACCEPTED, {"ok": True})
        elif path == "/api/tasks":
            plan = app.scheduler.save_task(body)
            app.events.publish("state")
            self._json(HTTPStatus.OK, {"id": plan.id})
        elif m := re.fullmatch(r"/api/tasks/(t[0-9a-f]{6})", path):
            app.scheduler.save_task(body, m.group(1))
            app.events.publish("state")
            self._json(HTTPStatus.OK, {"id": m.group(1)})
        elif m := re.fullmatch(r"/api/tasks/(t[0-9a-f]{6})/delete", path):
            app.scheduler.delete_task(m.group(1))
            app.events.publish("state")
            self._json(HTTPStatus.OK, {"ok": True})
        elif m := re.fullmatch(r"/api/tasks/(t[0-9a-f]{6})/run", path):
            app.scheduler.run_task_now(m.group(1))
            app.events.publish("state")
            self._json(HTTPStatus.ACCEPTED, {"ok": True})
        elif path == "/api/mail/read":
            ids = body.get("ids") or []
            if not isinstance(ids, list):
                raise ValueError("ids must be a list")
            app.mail.set_read([str(i) for i in ids], bool(body.get("read", True)))
            app.mail_watch.poll()
            self._json(HTTPStatus.OK, {"ok": True})
        elif path == "/api/mail/delete":
            if body.get("all") is True:
                app.mail.delete(None)
            else:
                ids = body.get("ids") or []
                if not isinstance(ids, list) or not ids:
                    raise ValueError("ids must be a non-empty list (or all: true)")
                app.mail.delete([str(i) for i in ids])
            app.mail_watch.poll()
            self._json(HTTPStatus.OK, {"ok": True})
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
