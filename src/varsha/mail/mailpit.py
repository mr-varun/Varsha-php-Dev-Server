"""Read varsha's Mailpit through its REST API, for the control panel's Mail view.

Mailpit keeps running exactly as before (PHP sends to its SMTP port, it stores the mail); varsha
only talks to its HTTP API on 127.0.0.1. The panel never reaches Mailpit directly: every call
goes through varsha's own server, so the panel's strict CSP and cookie login stay in force and
Mailpit's port is never part of the page.

Message HTML is untrusted (it's whatever an app under development sent), so `html()` prepares it
for a sandboxed iframe: inline (cid:) images become data: URIs, links open in a new tab, and the
server adds a CSP that allows no scripts and, unless asked, no remote images.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from varsha.errors import VarshaError
from varsha.services.manager import ServiceManager
from varsha.services.models import ServiceState

logger = logging.getLogger(__name__)

MAIL_TYPE = "mail"
ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
PART_RE = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,4}){0,9}")
MAX_INLINE_IMAGE = 2 * 1024 * 1024  # larger inline images are left out rather than bloating the page
MAX_LIMIT = 200


class MailError(VarshaError):
    pass


def check_id(message_id: str) -> str:
    if not ID_RE.fullmatch(message_id or ""):
        raise ValueError("bad message id")
    return message_id


def check_part(part_id: str) -> str:
    if not PART_RE.fullmatch(part_id or ""):
        raise ValueError("bad part id")
    return part_id


def _addr(entry: Any) -> dict[str, str]:
    entry = entry or {}
    return {"name": str(entry.get("Name") or ""), "address": str(entry.get("Address") or "")}


def _addrs(entries: Any) -> list[dict[str, str]]:
    return [_addr(e) for e in (entries or [])]


def _summary(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": m.get("ID", ""), "read": bool(m.get("Read")), "from": _addr(m.get("From")),
        "to": _addrs(m.get("To")), "subject": m.get("Subject") or "", "created": m.get("Created") or "",
        "size": int(m.get("Size") or 0), "attachments": int(m.get("Attachments") or 0),
        "snippet": m.get("Snippet") or "", "tags": list(m.get("Tags") or []),
    }


class MailpitClient:
    """A small client for the Mailpit service defined in services.json (type "mail")."""

    def __init__(self, manager: ServiceManager, timeout: float = 5.0) -> None:
        self.manager = manager
        self.timeout = timeout

    # ------------------------------------------------------------------ service

    @property
    def service_id(self) -> str | None:
        for sid in self.manager.service_ids:
            defn = self.manager.definition(sid)
            if defn.type == MAIL_TYPE and len(defn.ports) >= 2:
                return sid
        return None

    @property
    def available(self) -> bool:
        return self.service_id is not None

    @property
    def smtp_port(self) -> int | None:
        sid = self.service_id
        return self.manager.definition(sid).ports[0] if sid else None

    @property
    def base_url(self) -> str | None:
        sid = self.service_id
        return f"http://127.0.0.1:{self.manager.definition(sid).ports[1]}" if sid else None

    def running(self) -> bool:
        sid = self.service_id
        return bool(sid) and self.manager.status(sid).state == ServiceState.RUNNING

    # ------------------------------------------------------------------ HTTP

    def _request(self, method: str, path: str, body: Any = None, raw: bool = False) -> Any:
        base = self.base_url
        if not base:
            raise MailError("No mail service is configured (a service with \"type\": \"mail\" in services.json).")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - loopback only
                payload = response.read()
                if raw:
                    return payload, response.headers.get("Content-Type", "application/octet-stream")
                if not payload or not payload.strip().startswith((b"{", b"[")):
                    return payload.decode("utf-8", "replace").strip()
                return json.loads(payload)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise MailError("That email is gone (deleted in Mailpit?).") from None
            raise MailError(f"Mailpit answered {exc.code}: {exc.read()[:200].decode('utf-8', 'replace')}") from None
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise MailError("Mailpit isn't answering. Start it on the Stack page.") from exc

    # ------------------------------------------------------------------ reading

    def info(self) -> dict[str, Any]:
        data = self._request("GET", "/api/v1/info")
        return {"version": data.get("Version", ""), "total": int(data.get("Messages") or 0),
                "unread": int(data.get("Unread") or 0), "db_size": int(data.get("DatabaseSize") or 0)}

    def messages(self, start: int = 0, limit: int = 50, query: str = "") -> dict[str, Any]:
        start, limit = max(0, int(start)), max(1, min(MAX_LIMIT, int(limit)))
        params = {"start": start, "limit": limit}
        if query.strip():
            params["query"] = query.strip()[:200]
            path = "/api/v1/search?" + urllib.parse.urlencode(params)
        else:
            path = "/api/v1/messages?" + urllib.parse.urlencode(params)
        data = self._request("GET", path)
        return {"total": int(data.get("total") or 0), "unread": int(data.get("unread") or 0),
                "count": int(data.get("messages_count") or 0), "start": start,
                "messages": [_summary(m) for m in data.get("messages") or []]}

    def message(self, message_id: str) -> dict[str, Any]:
        """Full message details (Mailpit marks it read)."""
        m = self._request("GET", f"/api/v1/message/{check_id(message_id)}")
        return {
            "id": m.get("ID", ""), "message_id": m.get("MessageID", ""), "from": _addr(m.get("From")),
            "to": _addrs(m.get("To")), "cc": _addrs(m.get("Cc")), "bcc": _addrs(m.get("Bcc")),
            "reply_to": _addrs(m.get("ReplyTo")), "return_path": m.get("ReturnPath") or "",
            "subject": m.get("Subject") or "", "date": m.get("Date") or "", "tags": list(m.get("Tags") or []),
            "text": m.get("Text") or "", "has_html": bool(m.get("HTML")), "size": int(m.get("Size") or 0),
            "attachments": [{"part": a.get("PartID", ""), "name": a.get("FileName") or "attachment",
                             "type": a.get("ContentType") or "application/octet-stream", "size": int(a.get("Size") or 0)}
                            for a in m.get("Attachments") or []],
            "inline": len(m.get("Inline") or []),
        }

    def html(self, message_id: str) -> str:
        """The message HTML ready for the sandboxed reader frame."""
        m = self._request("GET", f"/api/v1/message/{check_id(message_id)}")
        html = m.get("HTML") or ""
        for part in m.get("Inline") or []:
            cid, part_id = part.get("ContentID") or "", part.get("PartID") or ""
            ctype = part.get("ContentType") or ""
            if not cid or not PART_RE.fullmatch(part_id) or not ctype.startswith("image/"):
                continue
            if int(part.get("Size") or 0) > MAX_INLINE_IMAGE:
                continue
            payload, _ = self._request("GET", f"/api/v1/message/{message_id}/part/{part_id}", raw=True)
            uri = f"data:{ctype};base64,{base64.b64encode(payload).decode('ascii')}"
            html = re.sub(r"cid:" + re.escape(cid) + r"(?=[\"'\s>)])", uri, html, flags=re.IGNORECASE)
        # Links open in a new tab (the frame is sandboxed with allow-popups only).
        base = '<base target="_blank">'
        if re.search(r"<head[^>]*>", html, flags=re.IGNORECASE):
            return re.sub(r"(<head[^>]*>)", r"\1" + base, html, count=1, flags=re.IGNORECASE)
        return base + html

    def headers(self, message_id: str) -> dict[str, list[str]]:
        data = self._request("GET", f"/api/v1/message/{check_id(message_id)}/headers")
        return {str(k): [str(v) for v in (vals or [])] for k, vals in sorted(data.items())}

    def raw(self, message_id: str) -> bytes:
        payload, _ = self._request("GET", f"/api/v1/message/{check_id(message_id)}/raw", raw=True)
        return payload

    def part(self, message_id: str, part_id: str) -> tuple[bytes, str, str]:
        """An attachment: (bytes, content type, file name)."""
        details = self._request("GET", f"/api/v1/message/{check_id(message_id)}")
        part_id = check_part(part_id)
        name, ctype = "attachment", "application/octet-stream"
        for a in (details.get("Attachments") or []) + (details.get("Inline") or []):
            if a.get("PartID") == part_id:
                name, ctype = a.get("FileName") or name, a.get("ContentType") or ctype
                break
        else:
            raise MailError("That attachment isn't in this email.")
        payload, _ = self._request("GET", f"/api/v1/message/{message_id}/part/{part_id}", raw=True)
        return payload, ctype, name

    # ------------------------------------------------------------------ changing

    def set_read(self, ids: list[str], read: bool) -> None:
        ids = [check_id(i) for i in ids]
        if not ids:
            # Mailpit treats an empty list as "every message".
            self._request("PUT", "/api/v1/messages", {"Read": bool(read)})
        else:
            self._request("PUT", "/api/v1/messages", {"IDs": ids, "Read": bool(read)})

    def delete(self, ids: list[str] | None) -> None:
        """Delete the given messages, or every message when ids is None."""
        if ids is None:
            self._request("DELETE", "/api/v1/messages", {})
            return
        ids = [check_id(i) for i in ids]
        if ids:
            self._request("DELETE", "/api/v1/messages", {"IDs": ids})


class MailWatcher:
    """Polls Mailpit's counters while it runs and reports changes and newly arrived mail.

    `on_change(data)` gets {"unread", "total", "new": [summaries of new messages]} whenever the
    counters change. The first poll after Mailpit (re)starts only sets the baseline, so old mail
    never shows up as "new".
    """

    def __init__(self, client: MailpitClient, on_change: Callable[[dict[str, Any]], None],
                 interval: float = 3.0) -> None:
        self.client = client
        self.on_change = on_change
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()  # poll() also runs on request threads (after read/delete)
        self._snapshot: dict[str, Any] = {"running": False, "unread": 0, "total": 0}
        self._baseline = False
        self._latest_id = ""

    @property
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snapshot)

    def start(self) -> None:
        if self._thread is None and self.client.available:
            self._thread = threading.Thread(target=self._run, name="mail-watch", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll()
            except Exception:  # never let the watcher die
                logger.exception("mail watcher poll failed")
            self._stop.wait(self.interval)

    def poll(self) -> None:
        with self._poll_lock:
            self._poll()

    def _poll(self) -> None:
        if not self.client.running():
            self._baseline = False
            self._set({"running": False, "unread": 0, "total": 0})
            return
        try:
            info = self.client.info()
        except MailError:
            self._baseline = False
            self._set({"running": False, "unread": 0, "total": 0})
            return
        new: list[dict[str, Any]] = []
        before = self.snapshot
        if not self._baseline or info["total"] != before["total"] or info["unread"] != before["unread"]:
            latest = self.client.messages(0, 10)["messages"] if info["total"] else []
            if self._baseline:
                for m in latest:
                    if m["id"] == self._latest_id:
                        break
                    if not m["read"]:
                        new.append(m)
            self._latest_id = latest[0]["id"] if latest else ""
            self._baseline = True
        self._set({"running": True, "unread": info["unread"], "total": info["total"]}, new)

    def _set(self, snapshot: dict[str, Any], new: list[dict[str, Any]] | None = None) -> None:
        with self._lock:
            changed = snapshot != self._snapshot
            self._snapshot = snapshot
        if changed or new:
            self.on_change({**snapshot, "new": new or []})
