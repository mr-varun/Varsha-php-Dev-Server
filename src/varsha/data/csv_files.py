"""CSV / TSV files on the Data page.

A CSV is parsed into a SQLite cache file (data/csv-cache/<id>.sqlite, one table named after the
file), so the Data page's SQLite code does the paging, sorting, search and SQL. The cache is
rebuilt whenever the file's size or modification time changes.

Edits go into the cache and are then written back to the CSV in the file's own format: the same
delimiter, quoting style, encoding (and BOM) and line endings, and the original header texts.
The original is copied to data/file-backups/ before the first change. A column is stored as a
number only if every value turns back into exactly the same text (so "007", "1200.50" or
"+5" stay text and are written back unchanged).
"""

from __future__ import annotations

import codecs
import csv
import io
import json
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from varsha.paths import AppPaths

logger = logging.getLogger(__name__)

CSV_EXTENSIONS = (".csv", ".tsv", ".tab", ".txt")
EDIT_LIMIT = 50 * 1024 * 1024  # bigger files open read-only
SAMPLE = 64 * 1024
DELIMITERS = ",;\t|"
DELIMITER_NAMES = {",": "comma", ";": "semicolon", "\t": "tab", "|": "pipe"}
csv.field_size_limit(64 * 1024 * 1024)


class CsvError(ValueError):
    pass


def detect_encoding(head: bytes) -> str:
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    try:
        head.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError as exc:
        if exc.start >= len(head) - 3:  # a multi-byte character cut at the end of the sample
            return "utf-8"
        return "cp1252"


def looks_like_text(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            head = handle.read(4096)
    except OSError:
        return False
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return True
    return b"\x00" not in head


def sniff(text: str, suffix: str) -> tuple[str, bool]:
    """(delimiter, has_header) from a sample of the file."""
    sample = text[:SAMPLE]
    default = "\t" if suffix in (".tsv", ".tab") else ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=DELIMITERS)
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = default
    if suffix in (".tsv", ".tab") and "\t" in sample:
        delimiter = "\t"
    try:
        header = csv.Sniffer().has_header(sample)
    except csv.Error:
        header = True
    first = next(csv.reader([sample.splitlines()[0] if sample else ""], delimiter=delimiter), [])
    if first and all(re.fullmatch(r"[A-Za-z_][\w .-]{0,60}", f.strip() or "-") for f in first):
        header = True  # names like id,name,city: the sniffer is often unsure about small files
    return delimiter, header


def _is_int(text: str) -> bool:
    try:
        return str(int(text)) == text
    except ValueError:
        return False


def _is_real(text: str) -> bool:
    try:
        return repr(float(text)) == text and text.lower() not in ("nan", "inf", "-inf")
    except ValueError:
        return False


def unique_names(headers: list[str]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for i, h in enumerate(headers, 1):
        base = h.strip() or f"column{i}"
        name, n = base, 2
        while name.lower() in seen:
            name, n = f"{base}_{n}", n + 1
        seen.add(name.lower())
        names.append(name)
    return names


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class CsvFiles:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.cache = paths.data / "csv-cache"
        self._lock = threading.Lock()

    def table_name(self, path: Path) -> str:
        return re.sub(r"\W+", "_", path.stem).strip("_") or "data"

    def _meta_file(self, file_id: str) -> Path:
        return self.cache / f"{file_id}.json"

    def meta(self, file_id: str, path: Path) -> dict[str, Any]:
        """Parse info for `path`, rebuilding the cache when the file changed."""
        with self._lock:
            stat = path.stat()
            try:
                meta = json.loads(self._meta_file(file_id).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                meta = {}
            db = self.cache / f"{file_id}.sqlite"
            if meta.get("size") != stat.st_size or meta.get("mtime") != stat.st_mtime_ns or not db.is_file():
                meta = self._build(file_id, path, db)
            return meta

    def db_path(self, file_id: str) -> Path:
        return self.cache / f"{file_id}.sqlite"

    def _build(self, file_id: str, path: Path, db: Path) -> dict[str, Any]:
        started = time.monotonic()
        raw = path.read_bytes()
        encoding = detect_encoding(raw[:SAMPLE])
        try:
            text = raw.decode(encoding)
        except UnicodeDecodeError:
            encoding = "cp1252"
            text = raw.decode(encoding, errors="replace")
        newline = "\r\n" if b"\r\n" in raw[:SAMPLE] else "\n"
        delimiter, has_header = sniff(text, path.suffix.lower())
        # newline="" keeps line breaks inside quoted fields (an address over two lines stays one value)
        rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=delimiter))
        if has_header and rows:
            headers = rows.pop(0)
        else:
            headers = []
        width = max([len(headers), *(len(r) for r in rows)], default=0)
        headers = headers + [f"column{i}" for i in range(len(headers) + 1, width + 1)] if has_header else \
            [f"column{i}" for i in range(1, width + 1)]
        names = unique_names(headers)
        kinds = []
        for i in range(width):
            values = [r[i] for r in rows if i < len(r) and r[i] != ""]
            if values and all(_is_int(v) for v in values):
                kinds.append("INTEGER")
            elif values and all(_is_int(v) or _is_real(v) for v in values):
                kinds.append("REAL")
            else:
                kinds.append("TEXT")
        self.cache.mkdir(parents=True, exist_ok=True)
        tmp = db.with_suffix(".building")
        tmp.unlink(missing_ok=True)
        conn = sqlite3.connect(tmp)
        try:
            table = self.table_name(path)
            cols = ", ".join(f"{quote(n)} {k}" for n, k in zip(names, kinds))
            conn.execute(f"CREATE TABLE {quote(table)} ({cols})" if cols else f"CREATE TABLE {quote(table)} (column1 TEXT)")

            def convert(row: list[str]) -> list[Any]:
                out: list[Any] = []
                for i in range(width):
                    v = row[i] if i < len(row) else ""
                    if kinds[i] == "INTEGER":
                        out.append(int(v) if v != "" else None)
                    elif kinds[i] == "REAL":
                        out.append((int(v) if _is_int(v) else float(v)) if v != "" else None)
                    else:
                        out.append(v)
                return out

            if width:
                conn.executemany(f"INSERT INTO {quote(table)} VALUES ({', '.join('?' * width)})", (convert(r) for r in rows))
            conn.commit()
        finally:
            conn.close()
        os.replace(tmp, db)
        stat = path.stat()
        meta = {"size": stat.st_size, "mtime": stat.st_mtime_ns, "encoding": encoding, "newline": newline,
                "delimiter": delimiter, "header": has_header, "headers": headers, "names": names, "kinds": kinds,
                "rows": len(rows), "table": self.table_name(path), "parsed": time.time(),
                "ms": round((time.monotonic() - started) * 1000)}
        self._meta_file(file_id).write_text(json.dumps(meta), encoding="utf-8")
        logger.info("Parsed %s: %s rows, %s columns, %r, %s", path, len(rows), width, delimiter, encoding)
        return meta

    def describe(self, meta: dict[str, Any]) -> dict[str, Any]:
        return {"encoding": {"utf-8-sig": "UTF-8 with BOM", "utf-8": "UTF-8", "cp1252": "Windows-1252",
                             "utf-16": "UTF-16"}.get(meta["encoding"], meta["encoding"]),
                "delimiter": DELIMITER_NAMES.get(meta["delimiter"], repr(meta["delimiter"])),
                "header": meta["header"], "newline": "CRLF" if meta["newline"] == "\r\n" else "LF"}

    def write_back(self, file_id: str, path: Path) -> None:
        """Write the cache table back to the CSV in the file's own format, then mark the cache current."""
        with self._lock:
            meta = json.loads(self._meta_file(file_id).read_text(encoding="utf-8"))
            conn = sqlite3.connect(self.db_path(file_id))
            try:
                info = conn.execute(f"PRAGMA table_info({quote(meta['table'])})").fetchall()
                names = [c[1] for c in info]
                cursor = conn.execute(f"SELECT * FROM {quote(meta['table'])} ORDER BY rowid")
                old_headers = dict(zip(meta["names"], meta["headers"]))
                tmp = path.with_name(path.name + ".varsha-new")
                with open(tmp, "w", encoding=meta["encoding"], newline="") as handle:
                    writer = csv.writer(handle, delimiter=meta["delimiter"], lineterminator=meta["newline"],
                                        quoting=csv.QUOTE_MINIMAL)
                    if meta["header"]:
                        writer.writerow([old_headers.get(n, n) for n in names])
                    for row in cursor:
                        writer.writerow(["" if v is None else (repr(v) if isinstance(v, float) else str(v)) for v in row])
            finally:
                conn.close()
            os.replace(tmp, path)
            stat = path.stat()
            meta.update(size=stat.st_size, mtime=stat.st_mtime_ns, names=names,
                        headers=[old_headers.get(n, n) for n in names])
            self._meta_file(file_id).write_text(json.dumps(meta), encoding="utf-8")

    def backup(self, path: Path, folder: Path) -> str:
        folder.mkdir(parents=True, exist_ok=True)
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
        target = folder / f"{stamp}{path.suffix}"
        n = 1
        while target.exists():
            target = folder / f"{stamp}-{n}{path.suffix}"
            n += 1
        shutil.copy2(path, target)
        return target.name

    def forget(self, file_id: str) -> None:
        for name in (f"{file_id}.json", f"{file_id}.sqlite"):
            (self.cache / name).unlink(missing_ok=True)
