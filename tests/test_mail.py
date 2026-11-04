"""Mail view: the Mailpit client, the new-mail watcher and the panel's /api/mail routes.

A fake Mailpit (stdlib HTTP server, same JSON shapes as Mailpit v1.22) stands in for the real one.
"""

from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from conftest import fake_def, free_port
from test_panel import Client

from varsha.config_engine.engine import ConfigEngine
from varsha.mail.mailpit import MailError, MailpitClient, MailWatcher, check_id
from varsha.secrets import CredentialStore
from varsha.services.models import ServiceDef, ServiceState
from varsha.services.remote import RemoteDbStore
from varsha.settings import Settings
from varsha.sites.manager import SiteManager
from varsha.tunnel.manager import TunnelManager
from varsha.web.panel import PanelApp
from varsha.web.server import bind_panel

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg+M/AwAAFAAH+p9aktwAAAABJRU5ErkJggg==")


# Shaped like Mailpit 1.22's real /html-check answer.
HTML_CHECK = {
    "Total": {"Tests": 181, "Nodes": 10, "Supported": 87.44681, "Partial": 2.5531917, "Unsupported": 10},
    "Platforms": {"ios": ["Apple Mail", "Outlook"], "windows": ["Outlook", "Mozilla Thunderbird"], "android": ["Gmail"]},
    "Warnings": [
        {"Slug": "css-width", "Title": "width", "Description": "", "URL": "https://www.caniemail.com/features/css-width/",
         "Category": "css", "Results": [{"Name": "Outlook Windows (2007)", "Support": "partial", "NoteNumber": "1"}],
         "NotesByNumber": {"1": "Partial. Replaced by a <code>&lt;div&gt;</code>."},
         "Score": {"Found": 1, "Supported": 0, "Partial": 12, "Unsupported": 0}},
        {"Slug": "html-script", "Title": "<script> element", "Description": "JavaScript is not supported in any email client.",
         "URL": "javascript:alert(1)", "Category": "html",
         "Results": [{"Name": "Outlook iOS (2021-11)", "Support": "no"}, {"Name": "Outlook iOS (2023-05)", "Support": "no"},
                     {"Name": "Gmail Android (2022-01)", "Support": "no"}],
         "NotesByNumber": {}, "Score": {"Found": 1, "Supported": 0, "Partial": 0, "Unsupported": 100}},
    ],
}


def make_message(i: int, read: bool = False) -> dict[str, Any]:
    return {
        "ID": f"msg{i:04d}", "MessageID": f"m{i}@mailpit", "Read": read,
        "From": {"Name": "Shop App", "Address": "noreply@shop.test"},
        "To": [{"Name": "Varun", "Address": "varun@example.com"}], "Cc": [], "Bcc": [], "ReplyTo": [],
        "ReturnPath": "noreply@shop.test", "Subject": f"Order #{i}", "Date": "2026-11-04T14:45:33+05:30",
        "Created": "2026-11-04T14:45:33+05:30", "Tags": [], "Size": 2000 + i, "Attachments": 1, "Snippet": f"Hello {i}",
        "Text": f"Hello {i}\n",
        "HTML": f'<html><head><title>x</title></head><body><img src="cid:logo{i}"><img src="https://tracker.example/p.png">'
                f'<a href="https://shop.test/{i}">go</a><script>alert(1)</script></body></html>',
        "Inline": [{"PartID": "1.2", "FileName": "logo", "ContentType": "image/png", "ContentID": f"logo{i}", "Size": len(PNG)}],
        "AttachmentList": [{"PartID": "2", "FileName": "invoice.csv", "ContentType": "text/csv", "ContentID": "", "Size": 12}],
    }


class FakeMailpit:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []  # newest first, like Mailpit
        self.calls: list[tuple[str, str, Any]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                pass

            def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, data: Any) -> None:
                self._send(200, json.dumps(data).encode())

            def _body(self) -> Any:
                length = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(length) or b"{}") if length else {}

            def _find(self, mid: str) -> dict[str, Any] | None:
                return next((m for m in fake.messages if m["ID"] == mid), None)

            def do_GET(self) -> None:  # noqa: N802
                parts = urlsplit(self.path)
                q = {k: v[0] for k, v in parse_qs(parts.query).items()}
                fake.calls.append(("GET", parts.path, q))
                path = parts.path.split("/")
                if parts.path == "/api/v1/info":
                    self._json({"Version": "v1.22.3", "Messages": len(fake.messages),
                                "Unread": sum(not m["Read"] for m in fake.messages), "DatabaseSize": 1})
                elif parts.path in ("/api/v1/messages", "/api/v1/search"):
                    found = fake.messages
                    if parts.path.endswith("search"):
                        found = [m for m in found if q.get("query", "") in m["Subject"]]
                    start, limit = int(q.get("start", 0)), int(q.get("limit", 50))
                    page = [{k: v for k, v in m.items() if k not in ("Text", "HTML", "Inline", "AttachmentList")} for m in found[start:start + limit]]
                    self._json({"total": len(fake.messages), "unread": sum(not m["Read"] for m in fake.messages),
                                "count": len(page), "messages_count": len(found), "start": start, "messages": page})
                elif len(path) >= 5 and path[3] == "message" and (m := self._find(path[4])):
                    if len(path) == 5:
                        m["Read"] = True
                        self._json({**{k: v for k, v in m.items() if k != "AttachmentList"}, "Attachments": m["AttachmentList"]})
                    elif path[5] == "headers":
                        self._json({"Subject": [m["Subject"]], "To": ["Varun <varun@example.com>"]})
                    elif path[5] == "raw":
                        self._send(200, b"Subject: " + m["Subject"].encode() + b"\r\n\r\nbody", "message/rfc822")
                    elif path[5] == "html-check":
                        self._json(HTML_CHECK)
                    elif path[5] == "link-check":
                        self._json({"Errors": 1, "Links": [
                            {"URL": "https://shop.test/ok", "StatusCode": 200, "Status": "OK"},
                            {"URL": "https://example.com/gone", "StatusCode": 404, "Status": "Not Found"}]})
                    elif path[5] == "part" and path[6] == "1.2":
                        self._send(200, PNG, "image/png")
                    elif path[5] == "part" and path[6] == "2":
                        self._send(200, b"invoice,1.00", "text/csv")
                    else:
                        self._send(404, b"not found", "text/plain")
                else:
                    self._send(404, b"not found", "text/plain")

            def do_PUT(self) -> None:  # noqa: N802
                body = self._body()
                fake.calls.append(("PUT", self.path, body))
                for m in fake.messages:
                    if not body.get("IDs") or m["ID"] in body["IDs"]:
                        m["Read"] = body.get("Read", True)
                self._send(200, b"ok", "text/plain")

            def do_DELETE(self) -> None:  # noqa: N802
                body = self._body()
                fake.calls.append(("DELETE", self.path, body))
                ids = body.get("IDs")
                fake.messages = [m for m in fake.messages if ids and m["ID"] not in ids]
                self._send(200, b"ok", "text/plain")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def add(self, i: int, read: bool = False) -> None:
        self.messages.insert(0, make_message(i, read))


class StubStatus:
    def __init__(self, state: ServiceState) -> None:
        self.state = state


class StubManager:
    """Just what MailpitClient needs: definitions and a state."""

    def __init__(self, http_port: int) -> None:
        self.defn = ServiceDef(id="mailpit", name="Mailpit", type="mail", exe="bin/mailpit/mailpit.exe",
                               ports=(free_port(), http_port))
        self.state = ServiceState.RUNNING

    @property
    def service_ids(self) -> list[str]:  # a property, like ServiceManager.service_ids
        return ["mailpit"]

    def definition(self, sid: str) -> ServiceDef:
        assert sid == "mailpit"
        return self.defn

    def status(self, sid: str) -> StubStatus:
        return StubStatus(self.state)


@pytest.fixture
def mailpit() -> Iterator[FakeMailpit]:
    fake = FakeMailpit()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def client(mailpit: FakeMailpit) -> MailpitClient:
    return MailpitClient(StubManager(mailpit.port))  # type: ignore[arg-type]


# ---------------------------------------------------------------- client

def test_lists_and_searches(mailpit: FakeMailpit, client: MailpitClient) -> None:
    for i in range(1, 6):
        mailpit.add(i, read=i < 3)
    page = client.messages(0, 2)
    assert page["total"] == 5 and page["unread"] == 3 and len(page["messages"]) == 2
    first = page["messages"][0]
    assert first["id"] == "msg0005" and first["subject"] == "Order #5" and first["from"]["address"] == "noreply@shop.test"
    assert not first["read"] and first["attachments"] == 1
    assert client.messages(0, 10, "#3")["count"] == 1
    assert client.messages(0, 10_000)["start"] == 0  # limit is clamped, not passed through
    assert ("GET", "/api/v1/messages", {"start": "0", "limit": "200"}) in mailpit.calls


def test_html_inlines_cid_images_and_opens_links_in_new_tab(mailpit: FakeMailpit, client: MailpitClient) -> None:
    mailpit.add(7)
    html = client.html("msg0007")
    assert "cid:logo7" not in html
    assert "data:image/png;base64," + base64.b64encode(PNG).decode() in html
    assert '<head><base target="_blank">' in html
    assert "https://tracker.example/p.png" in html  # left alone: the frame's CSP blocks it unless allowed


def test_message_details_parts_and_headers(mailpit: FakeMailpit, client: MailpitClient) -> None:
    mailpit.add(1)
    details = client.message("msg0001")
    assert details["has_html"] and details["text"] == "Hello 1\n" and details["inline"] == 1
    assert details["attachments"] == [{"part": "2", "name": "invoice.csv", "type": "text/csv", "size": 12}]
    body, ctype, name = client.part("msg0001", "2")
    assert (body, ctype, name) == (b"invoice,1.00", "text/csv", "invoice.csv")
    with pytest.raises(MailError):
        client.part("msg0001", "9")
    assert client.headers("msg0001")["Subject"] == ["Order #1"]
    assert client.raw("msg0001").startswith(b"Subject: Order #1")
    with pytest.raises(MailError, match="gone"):
        client.message("nope0000")


def test_ids_are_validated() -> None:
    for bad in ("", "../etc", "a/b", "x" * 65, "id with space"):
        with pytest.raises(ValueError):
            check_id(bad)


def test_read_and_delete(mailpit: FakeMailpit, client: MailpitClient) -> None:
    for i in range(1, 4):
        mailpit.add(i)
    client.set_read(["msg0001"], True)
    assert mailpit.calls[-1] == ("PUT", "/api/v1/messages", {"IDs": ["msg0001"], "Read": True})
    client.set_read([], True)  # all
    assert mailpit.calls[-1] == ("PUT", "/api/v1/messages", {"Read": True})
    client.delete(["msg0002"])
    assert [m["ID"] for m in mailpit.messages] == ["msg0003", "msg0001"]
    client.delete([])  # an empty list must never mean "everything"
    assert len(mailpit.messages) == 2
    client.delete(None)
    assert mailpit.messages == []


def test_stopped_or_unreachable_mailpit_is_a_clear_error(client: MailpitClient, mailpit: FakeMailpit) -> None:
    mailpit.server.shutdown()
    mailpit.server.server_close()
    with pytest.raises(MailError, match="isn't answering"):
        client.info()


# ---------------------------------------------------------------- watcher

def test_watcher_reports_only_mail_that_arrives_after_the_baseline(mailpit: FakeMailpit, client: MailpitClient) -> None:
    mailpit.add(1)
    events: list[dict[str, Any]] = []
    watcher = MailWatcher(client, events.append)
    watcher.poll()
    assert events[-1]["new"] == [] and events[-1]["unread"] == 1  # old mail is not "new"
    watcher.poll()
    assert len(events) == 1  # nothing changed, nothing sent
    mailpit.add(2)
    mailpit.add(3)
    watcher.poll()
    assert [m["id"] for m in events[-1]["new"]] == ["msg0003", "msg0002"] and events[-1]["total"] == 3
    client.manager.state = ServiceState.STOPPED  # type: ignore[attr-defined]
    watcher.poll()
    assert events[-1] == {"running": False, "unread": 0, "total": 0, "new": []}
    client.manager.state = ServiceState.RUNNING  # type: ignore[attr-defined]
    watcher.poll()
    assert events[-1]["new"] == []  # a restart sets a new baseline


# ---------------------------------------------------------------- panel routes

@pytest.fixture
def panel_mail(app_paths, make_manager, mailpit: FakeMailpit) -> Iterator[tuple[PanelApp, Client, FakeMailpit]]:  # type: ignore[no-untyped-def]
    manager = make_manager([fake_def("svc", free_port())])
    settings = Settings()
    creds = CredentialStore(app_paths.data / "credentials.json")
    remote = RemoteDbStore(app_paths.config / "remote-databases.json", creds)
    engine = ConfigEngine(app_paths, settings, [manager.definition("svc")], remote=remote)
    app = PanelApp(app_paths, settings, manager, engine, SiteManager(engine, manager), TunnelManager(app_paths), remote=remote)
    app._dns_route_cache = (float("inf"), False)  # noqa: SLF001
    app.mail = MailpitClient(StubManager(mailpit.port))  # type: ignore[arg-type]
    app.mail_watch = MailWatcher(app.mail, lambda data: app.events.publish("mail", data))
    server = bind_panel(app, free_port())
    server.start()
    web = Client(server)
    web.login()
    yield app, web, mailpit
    server.shutdown()
    app.shutdown()


def test_panel_lists_and_reads_mail(panel_mail) -> None:  # type: ignore[no-untyped-def]
    app, web, mailpit = panel_mail
    mailpit.add(1)
    status, _, body = web.request("GET", "/api/mail/messages?limit=5")
    assert status == 200 and json.loads(body)["messages"][0]["id"] == "msg0001"
    status, _, body = web.request("GET", "/api/mail/message/msg0001")
    assert status == 200 and json.loads(body)["subject"] == "Order #1"
    assert json.loads(web.request("GET", "/api/state")[2])["mail"]["unread"] == 0  # opening it marked it read
    assert web.request("GET", "/api/mail/message/..%2Fetc")[0] == 404


def test_mail_html_is_sandboxed_and_only_framed_by_the_panel(panel_mail) -> None:  # type: ignore[no-untyped-def]
    _app, web, mailpit = panel_mail
    mailpit.add(1)
    status, headers, body = web.request("GET", "/api/mail/message/msg0001/html")
    csp = headers["Content-Security-Policy"]
    assert status == 200 and b"data:image/png;base64," in body
    assert "sandbox allow-popups" in csp and "script-src" not in csp and "default-src 'none'" in csp
    assert "img-src data:;" in csp and "frame-ancestors 'self'" in csp and headers["X-Frame-Options"] == "SAMEORIGIN"
    _, headers, _ = web.request("GET", "/api/mail/message/msg0001/html?remote=1")
    assert "img-src data: https: http:;" in headers["Content-Security-Policy"]
    # the panel page itself still refuses to be framed
    _, headers, _ = web.request("GET", "/")
    assert headers["X-Frame-Options"] == "DENY"


def test_attachments_and_raw_are_downloads(panel_mail) -> None:  # type: ignore[no-untyped-def]
    _app, web, mailpit = panel_mail
    mailpit.add(1)
    status, headers, body = web.request("GET", "/api/mail/message/msg0001/part/2")
    assert status == 200 and body == b"invoice,1.00"
    assert headers["Content-Disposition"] == 'attachment; filename="invoice.csv"' and headers["Content-Type"] == "text/csv"
    status, headers, _ = web.request("GET", "/api/mail/message/msg0001/raw")
    assert status == 200 and headers["Content-Disposition"] == 'attachment; filename="msg0001.eml"'
    assert web.request("GET", "/api/mail/message/msg0001/part/9")[0] == 409


def test_mail_actions_need_the_panel_header_and_explicit_ids(panel_mail) -> None:  # type: ignore[no-untyped-def]
    _app, web, mailpit = panel_mail
    for i in range(1, 4):
        mailpit.add(i)
    assert web.request("POST", "/api/mail/delete", {"all": True})[0] == 403  # no X-Varsha header
    assert web.post("/api/mail/delete", {"ids": []})[0] == 400
    assert web.post("/api/mail/delete", {"all": "yes"})[0] == 400  # must be literally true
    assert web.post("/api/mail/delete", {"ids": ["msg0002"]})[0] == 200
    assert [m["ID"] for m in mailpit.messages] == ["msg0003", "msg0001"]
    assert web.post("/api/mail/read", {"ids": ["msg0003"], "read": True})[0] == 200
    assert mailpit.messages[0]["Read"] is True
    assert web.post("/api/mail/delete", {"all": True})[0] == 200
    assert mailpit.messages == []


def test_html_compatibility_report(mailpit: FakeMailpit, client: MailpitClient) -> None:
    mailpit.add(1)
    report = client.html_check("msg0001")
    assert (report["tests"], report["supported"], report["unsupported"], report["apps"]) == (181, 87.4, 10.0, 4)
    first, second = report["warnings"]
    assert first["slug"] == "html-script" and first["unsupported"] == 100  # worst first
    assert first["title"] == "<script> element"  # a literal tag name in a title stays (the panel escapes it)
    assert first["not_in"] == ["Gmail Android", "Outlook iOS"]  # versions merged into one app
    assert first["url"] == ""  # only caniemail.com links are passed on
    assert second["notes"] == ["Partial. Replaced by a <div>."]  # their HTML becomes plain text
    assert second["partly_in"] == ["Outlook Windows"] and second["url"].startswith("https://www.caniemail.com/")


def test_link_check_lists_broken_links_first(mailpit: FakeMailpit, client: MailpitClient) -> None:
    mailpit.add(1)
    result = client.link_check("msg0001", follow=True)
    assert result["errors"] == 1 and [l["code"] for l in result["links"]] == [404, 200]
    assert ("GET", "/api/v1/message/msg0001/link-check", {"follow": "true"}) in mailpit.calls
    client.link_check("msg0001")
    assert ("GET", "/api/v1/message/msg0001/link-check", {}) in mailpit.calls


def test_panel_routes_for_the_checks(panel_mail) -> None:  # type: ignore[no-untyped-def]
    _app, web, mailpit = panel_mail
    mailpit.add(1)
    status, _, body = web.request("GET", "/api/mail/message/msg0001/html-check")
    assert status == 200 and json.loads(body)["warnings"][0]["slug"] == "html-script"
    status, _, body = web.request("GET", "/api/mail/message/msg0001/link-check?follow=1")
    assert status == 200 and json.loads(body)["errors"] == 1
