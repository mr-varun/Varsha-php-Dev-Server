"""PHP error watcher: follows PHP's error logs live and reports new errors.

Every PHP build writes to logs/php-<version>.log (php.ini error_log); errors that reach Apache
instead (mod_fcgid "stderr: PHP ..." lines in logs/apache-error.log) are picked up too. Entries are
parsed (the level, message, file and line; continuation lines such as a stack trace belong to the
entry above), grouped by level + message + file + line so a repeating error is one row with a
count, and mapped to the site whose project folder contains the file.

New entries go to `on_event` (the panel's "php_error" event); entries at the configured notify
level also carry a short text for the tray balloon. A group that keeps repeating is announced at
most once per REPEAT_QUIET seconds. Old log content is read once at start as history, never
announced. The settings (notify level, muted groups) live in config/php-errors.json.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ENTRY_RE = re.compile(r"^\[(?P<when>[^\]]{10,40})\] PHP (?P<level>[A-Za-z ]+?):\s+(?P<message>.*)$")
FCGID_RE = re.compile(r"mod_fcgid: stderr: PHP (?P<level>[A-Za-z ]+?):\s+(?P<message>.*?)(?:, referer: .*)?$")
WHERE_RE = re.compile(r"\s+in (?P<file>.+?)(?: on line (?P<line>\d+)|:(?P<line2>\d+))\s*$")
THROWN_RE = re.compile(r"^\s*thrown in (?P<file>.+?) on line (?P<line>\d+)\s*$")
SEVERITY = {  # PHP's level names -> error | warning | notice
    "fatal error": "error", "parse error": "error", "recoverable fatal error": "error", "core error": "error",
    "compile error": "error", "user error": "error", "catchable fatal error": "error",
    "warning": "warning", "core warning": "warning", "compile warning": "warning", "user warning": "warning",
    "notice": "notice", "deprecated": "notice", "strict standards": "notice", "user notice": "notice",
    "user deprecated": "notice",
}
NOTIFY_LEVELS = {"errors": ("error",), "warnings": ("error", "warning"), "off": ()}
MAX_GROUPS = 400
HISTORY_BYTES = 256 * 1024
REPEAT_QUIET = 60.0


@dataclass
class ErrorGroup:
    id: str
    severity: str
    level: str
    message: str
    file: str
    line: int
    site: str
    source: str
    detail: str = ""  # the full text, stack trace included
    count: int = 1
    first_seen: float = 0.0
    last_seen: float = 0.0
    seen: bool = False
    announced: float = 0.0  # when a balloon/toast last went out for it


@dataclass
class _Entry:
    level: str
    message: str
    lines: list[str] = field(default_factory=list)


def parse_lines(lines: Iterable[str], fcgid: bool = False) -> list[_Entry]:
    """PHP error-log text -> entries (continuation lines join the entry above)."""
    entries: list[_Entry] = []
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        m = (FCGID_RE.search(line) if fcgid else ENTRY_RE.match(line))
        if m:
            entries.append(_Entry(m.group("level").strip(), m.group("message").strip(), [m.group("message").strip()]))
        elif entries and not fcgid and not line.startswith("["):
            entries[-1].lines.append(line)
    return entries


def locate(entry: _Entry) -> tuple[str, str, int]:
    """(message without the location, file, line) for an entry."""
    message, file, line = entry.message, "", 0
    m = WHERE_RE.search(message)
    if m:
        message = message[: m.start()].strip()
        file, line = m.group("file").strip(), int(m.group("line") or m.group("line2") or 0)
    for extra in entry.lines[1:]:  # "  thrown in X on line N" points at the real place
        t = THROWN_RE.match(extra)
        if t:
            file, line = t.group("file").strip(), int(t.group("line"))
    return message, file, line


def group_id(severity: str, message: str, file: str, line: int) -> str:
    # Numbers inside messages (ids, memory sizes, timestamps) would split one bug into many rows.
    normal = re.sub(r"\d+", "#", message)[:300]
    return hashlib.sha1(f"{severity}|{normal}|{file.lower()}|{line}".encode("utf-8")).hexdigest()[:12]


class PhpErrorWatcher:
    def __init__(self, logs_dir: Path, settings_file: Path,
                 site_roots: Callable[[], list[tuple[str, Path]]],
                 on_event: Callable[[dict[str, Any]], None], interval: float = 1.0) -> None:
        self.logs_dir = logs_dir
        self.settings_file = settings_file
        self.site_roots = site_roots
        self.on_event = on_event
        self.interval = interval
        self._groups: OrderedDict[str, ErrorGroup] = OrderedDict()
        self._offsets: dict[Path, int] = {}
        self._heads: dict[Path, bytes] = {}  # first bytes of each log, to notice a replaced file
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._roots: list[tuple[str, Path]] = []
        self._roots_at = 0.0
        self.settings = self._load_settings()

    # ------------------------------------------------------------------ settings

    def _load_settings(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        notify = raw.get("notify") if raw.get("notify") in NOTIFY_LEVELS else "errors"
        muted = [m for m in raw.get("muted", []) if isinstance(m, str) and re.fullmatch(r"[0-9a-f]{12}", m)]
        return {"notify": notify, "muted": muted}

    def _save_settings(self) -> None:
        self.settings_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"_comment": "PHP error watcher; edit in the varsha panel (Errors).",
                                   **self.settings}, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.settings_file)

    def set_notify(self, level: str) -> None:
        if level not in NOTIFY_LEVELS:
            raise ValueError(f"notify must be one of {', '.join(NOTIFY_LEVELS)}")
        with self._lock:
            self.settings["notify"] = level
            self._save_settings()

    # ------------------------------------------------------------------ files

    def _sources(self) -> list[tuple[Path, bool]]:
        files = [(p, False) for p in sorted(self.logs_dir.glob("php-*.log"))]
        apache = self.logs_dir / "apache-error.log"
        if apache.is_file():
            files.append((apache, True))
        return files

    def _site_for(self, file: str) -> str:
        if not file:
            return ""
        now = time.monotonic()
        if now - self._roots_at > 30:
            roots = []
            for domain, folder in self.site_roots():
                try:
                    real = folder.resolve()
                except OSError:
                    continue
                # Sites often point at a public\ folder: the project (and its code) is one level up.
                roots.append((domain, real.parent if real.name.lower() == "public" else real))
            self._roots = sorted(roots, key=lambda r: len(str(r[1])), reverse=True)
            self._roots_at = now
        target = os.path.normcase(os.path.abspath(file))
        for domain, root in self._roots:
            base = os.path.normcase(str(root))
            if target == base or target.startswith(base.rstrip("\\/") + os.sep):
                return domain
        return ""

    def _read_new(self, path: Path, fcgid: bool, history: bool) -> list[_Entry]:
        try:
            size = path.stat().st_size
        except OSError:
            return []
        offset = self._offsets.get(path)
        if offset is None:
            # At start: the recent tail is history. A log that appears later (a PHP version's first
            # error ever) is new from its first byte.
            offset = max(0, size - HISTORY_BYTES) if history else 0
        with open(path, "rb") as stream:
            head = stream.read(64)
            # Rotated, cleared or replaced: smaller than before, or it starts differently.
            if size < offset or (path in self._heads and head[: len(self._heads[path])] != self._heads[path]):
                offset = 0
            self._heads[path] = head
            if size == offset:
                self._offsets[path] = offset
                return []
            stream.seek(offset)
            data = stream.read(min(size - offset, 4 * 1024 * 1024))
        # Only complete lines: a half-written entry is read on the next pass.
        cut = data.rfind(b"\n")
        if cut < 0:
            return []
        self._offsets[path] = offset + cut + 1
        text = data[: cut + 1].decode("utf-8", errors="replace")
        if offset and history:
            text = text.split("\n", 1)[1] if "\n" in text else ""  # history starts mid-file: skip the partial line
        return parse_lines(text.splitlines(), fcgid)

    # ------------------------------------------------------------------ running

    def start(self) -> None:
        if self._thread is None:
            self.poll(history=True)
            self._thread = threading.Thread(target=self._run, name="php-errors", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.poll()
            except Exception:
                logger.exception("PHP error watcher poll failed")

    def poll(self, history: bool = False) -> list[ErrorGroup]:
        """Read what's new in every log; returns the groups that changed (for tests)."""
        changed: dict[str, ErrorGroup] = {}
        announce: list[ErrorGroup] = []
        now = time.time()
        with self._lock:
            notify_levels = NOTIFY_LEVELS[self.settings["notify"]]
            for path, fcgid in self._sources():
                for entry in self._read_new(path, fcgid, history):
                    severity = SEVERITY.get(entry.level.lower(), "warning")
                    message, file, line = locate(entry)
                    gid = group_id(severity, message, file, line)
                    group = self._groups.get(gid)
                    if group is None:
                        group = ErrorGroup(gid, severity, entry.level, message, file, line, self._site_for(file),
                                           path.name, "\n".join(entry.lines), 1, now, now, seen=history)
                        self._groups[gid] = group
                    else:
                        group.count += 1
                        group.last_seen = now
                        group.detail = "\n".join(entry.lines)
                        if not history:
                            group.seen = False
                        self._groups.move_to_end(gid)
                    changed[gid] = group
                    if (not history and severity in notify_levels and gid not in self.settings["muted"]
                            and now - group.announced > REPEAT_QUIET):
                        group.announced = now
                        announce.append(group)
            while len(self._groups) > MAX_GROUPS:
                self._groups.popitem(last=False)
        if changed and not history:
            self.on_event({"new": [self._public(g) for g in announce], "summary": self.summary()})
        return list(changed.values())

    # ------------------------------------------------------------------ panel

    def _public(self, g: ErrorGroup) -> dict[str, Any]:
        data = asdict(g)
        data["muted"] = g.id in self.settings["muted"]
        data["file_name"] = Path(g.file).name if g.file else ""
        return data

    def summary(self) -> dict[str, Any]:
        groups = list(self._groups.values())
        unseen = [g for g in groups if not g.seen and g.id not in self.settings["muted"]]
        return {"unseen_errors": sum(g.severity == "error" for g in unseen),
                "unseen_warnings": sum(g.severity == "warning" for g in unseen),
                "unseen": len(unseen), "groups": len(groups), "notify": self.settings["notify"]}

    def groups(self, severity: str = "", site: str = "", query: str = "") -> list[dict[str, Any]]:
        q = query.strip().lower()
        with self._lock:
            out = [self._public(g) for g in reversed(self._groups.values())
                   if (not severity or g.severity == severity) and (not site or g.site == site)
                   and (not q or q in g.message.lower() or q in g.file.lower() or q in g.site.lower())]
        return sorted(out, key=lambda g: g["last_seen"], reverse=True)

    def group(self, gid: str) -> ErrorGroup:
        with self._lock:
            if gid not in self._groups:
                raise KeyError("That error is gone (cleared?)")
            return self._groups[gid]

    def mark_seen(self) -> None:
        with self._lock:
            for g in self._groups.values():
                g.seen = True

    def clear(self) -> None:
        with self._lock:
            self._groups.clear()

    def dismiss(self, gid: str) -> None:
        with self._lock:
            self._groups.pop(gid, None)

    def set_muted(self, gid: str, muted: bool) -> None:
        with self._lock:
            muted_ids = [m for m in self.settings["muted"] if m != gid]
            if muted:
                muted_ids.append(gid)
            self.settings["muted"] = muted_ids[-500:]
            self._save_settings()
