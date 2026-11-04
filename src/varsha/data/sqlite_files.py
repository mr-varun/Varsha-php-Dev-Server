"""The Data page's SQLite editor: browse, search, edit and query .db / .sqlite files.

Safety rules (they matter: these are often the live database of a site):
- Files are picked in a native Windows dialog (or from the recent list / the sites scan); the
  panel only ever sends an opaque file id, never a path.
- Every file opens read-only (SQLite's mode=ro). Changing anything needs Edit mode, which is
  per file and ends when varsha restarts.
- Before the first change in a session, a consistent copy is saved with SQLite's backup API
  (works with WAL files too) to data/file-backups/<name>-<id>/, and Restore puts one back.
- Table and column names are looked up in the file's own schema before they're used in SQL,
  and are always quoted. ATTACH / VACUUM INTO (which could write other files) are refused.
- Files inside varsha's data folder (the MySQL/MariaDB data, credentials) are never opened.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from varsha.data.access_files import ACCESS_EXTENSIONS, AccessError, AccessReader, engine_name, is_access
from varsha.paths import AppPaths

logger = logging.getLogger(__name__)

MAGIC = b"SQLite format 3\x00"
EXTENSIONS = (".db", ".sqlite", ".sqlite3", ".db3", ".s3db", ".sl3")
MAX_RECENT = 12
PAGE_SIZE = 100
MAX_PAGE = 500
SQL_MAX_ROWS = 1000
TEXT_PREVIEW = 2000  # longer text is cut in the grid; the edit dialog loads the whole value
QUERY_SECONDS = 30
KEEP_BACKUPS = 20
SCAN_SKIP = {"node_modules", "vendor", ".git", ".svn", ".idea", ".vscode", "storage", "cache", "logs"}
SCAN_DEPTH = 3
SCAN_LIMIT = 50
TYPES = ("TEXT", "INTEGER", "REAL", "NUMERIC", "BLOB", "")


class DataError(ValueError):
    """Shown to the user as is (HTTP 400)."""


def quote(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def is_sqlite(path: Path) -> bool:
    try:
        with open(path, "rb") as handle:
            return handle.read(16) == MAGIC
    except OSError:
        return False


def split_statements(sql: str) -> list[str]:
    """Split a script into statements; quotes, comments and triggers are respected because
    sqlite3.complete_statement() decides where one ends."""
    statements: list[str] = []
    current = ""
    pieces = sql.split(";")
    for i, piece in enumerate(pieces):
        current += piece + (";" if i < len(pieces) - 1 else "")
        if sqlite3.complete_statement(current):
            text = current.strip()
            if text.strip(";").strip():
                statements.append(text)
            current = ""
    rest = current.strip().rstrip(";").strip()
    if rest:
        statements.append(rest)
    return statements


def cell(value: Any) -> dict[str, Any]:
    """A value with its SQLite storage class, safe for JSON and JavaScript numbers."""
    if value is None:
        return {"t": "null"}
    if isinstance(value, bool):
        value = int(value)
    if isinstance(value, int):
        return {"t": "int", "v": value} if abs(value) <= 2**53 else {"t": "int", "v": str(value), "big": True}
    if isinstance(value, float):
        return {"t": "real", "v": value if value == value and abs(value) != float("inf") else str(value)}
    if isinstance(value, (bytes, memoryview)):
        data = bytes(value)
        return {"t": "blob", "size": len(data), "hex": data[:24].hex()}
    text = str(value)
    if len(text) > TEXT_PREVIEW:
        return {"t": "text", "v": text[:TEXT_PREVIEW], "more": len(text)}
    return {"t": "text", "v": text}


def from_cell(data: Any) -> Any:
    """What the panel sends back: {"t": "null"} | {"t": "text"|"int"|"real", "v": ...}."""
    if not isinstance(data, dict) or data.get("t") not in ("null", "text", "int", "real"):
        raise DataError("A value must be NULL, text, an integer or a real number")
    kind, value = data["t"], data.get("v")
    if kind == "null":
        return None
    if kind == "text":
        return "" if value is None else str(value)
    try:
        return int(str(value).strip()) if kind == "int" else float(str(value).strip())
    except ValueError:
        raise DataError(f"{value!r} isn't a valid {'integer' if kind == 'int' else 'number'}") from None


def _authorizer(action: int, *_args: Any) -> int:
    # ATTACH (and VACUUM INTO, which attaches its target) could read or write any file.
    if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class DataFiles:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self._recent_file = paths.data / "data-recent.json"
        self._files: dict[str, Path] = {}
        self._edit: set[str] = set()
        self._backed_up: set[str] = set()
        self._lock = threading.Lock()
        self.access = AccessReader(paths)
        for entry in self._load_recent():
            self._files[entry["id"]] = Path(entry["path"])

    # ------------------------------------------------------------------ which files

    @staticmethod
    def file_id(path: Path) -> str:
        return hashlib.sha256(str(path).lower().encode("utf-8")).hexdigest()[:16]

    def check_path(self, path: Path) -> Path:
        try:
            path = path.resolve(strict=True)
        except OSError:
            raise DataError(f"{path} doesn't exist") from None
        if not path.is_file():
            raise DataError(f"{path} isn't a file")
        data = self.paths.data.resolve()
        if path == data or data in path.parents:
            raise DataError("Files inside varsha's data folder (server databases, credentials) can't be opened here")
        if not is_sqlite(path) and not is_access(path):
            raise DataError(f"{path.name} isn't a SQLite or Access database (a .db file can also be Paradox or another format)")
        return path

    def kind(self, file_id: str) -> str:
        return "access" if is_access(self.path(file_id)) else "sqlite"

    def _read_only_access(self, file_id: str) -> None:
        if self.kind(file_id) == "access":
            raise DataError("Access files are read-only in varsha (browse, search, query and export only)")

    def register(self, raw: str) -> dict[str, Any]:
        """A file the user picked: check it, remember it, return its id."""
        path = self.check_path(Path(raw))
        file_id = self.file_id(path)
        with self._lock:
            self._files[file_id] = path
            recent = [e for e in self._load_recent() if e["id"] != file_id]
            recent.insert(0, {"id": file_id, "path": str(path)})
            self._save_recent(recent[:MAX_RECENT])
        return {"id": file_id, "name": path.name, "path": str(path), "kind": self.kind(file_id)}

    def forget(self, file_id: str) -> None:
        with self._lock:
            self._save_recent([e for e in self._load_recent() if e["id"] != file_id])
            self._edit.discard(file_id)
        self.access.forget(file_id)

    def path(self, file_id: str) -> Path:
        path = self._files.get(file_id)
        if path is None:  # e.g. the recent list was changed by another varsha session
            entry = next((e for e in self._load_recent() if e["id"] == file_id), None)
            if entry is None:
                raise DataError("Unknown file: open it again")
            path = self._files[file_id] = Path(entry["path"])
        return self.check_path(path)

    def _load_recent(self) -> list[dict[str, str]]:
        try:
            data = json.loads(self._recent_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [e for e in data if isinstance(e, dict) and isinstance(e.get("id"), str) and isinstance(e.get("path"), str)] \
            if isinstance(data, list) else []

    def _save_recent(self, entries: list[dict[str, str]]) -> None:
        self._recent_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._recent_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        os.replace(tmp, self._recent_file)

    def listing(self) -> dict[str, Any]:
        recent = []
        for entry in self._load_recent():
            path = Path(entry["path"])
            try:
                stat = path.stat()
                recent.append({"id": entry["id"], "name": path.name, "path": str(path), "size": stat.st_size,
                               "modified": stat.st_mtime, "exists": True})
            except OSError:
                recent.append({"id": entry["id"], "name": path.name, "path": str(path), "exists": False})
        return {"recent": recent, "found": self.found_in_sites()}

    def found_in_sites(self) -> list[dict[str, Any]]:
        """SQLite files inside the sites (e.g. a Laravel database/database.sqlite)."""
        found: list[dict[str, Any]] = []
        www = self.paths.www
        if not www.is_dir():
            return found

        def walk(folder: Path, depth: int, site: str) -> None:
            if len(found) >= SCAN_LIMIT or depth > SCAN_DEPTH:
                return
            try:
                entries = sorted(folder.iterdir(), key=lambda p: p.name.lower())
            except OSError:
                return
            for entry in entries:
                try:
                    if entry.is_dir():
                        if entry.name.lower() not in SCAN_SKIP and not entry.name.startswith("."):
                            walk(entry, depth + 1, site)
                    elif (entry.suffix.lower() in EXTENSIONS and is_sqlite(entry)) or \
                            (entry.suffix.lower() in ACCESS_EXTENSIONS and is_access(entry)):
                        path = entry.resolve()
                        file_id = self.file_id(path)
                        self._files.setdefault(file_id, path)
                        found.append({"id": file_id, "name": entry.name, "path": str(path), "site": site,
                                      "size": entry.stat().st_size})
                except OSError:
                    continue

        for site in sorted(www.iterdir(), key=lambda p: p.name.lower()):
            if site.is_dir():
                walk(site, 1, site.name)
                # a link to <project>/public: the database usually lives in the project, one level up
                if site.resolve().name.lower() == "public":
                    walk(site.resolve().parent, 1, site.name)
        unique: dict[str, dict[str, Any]] = {}
        for item in found:
            unique.setdefault(item["id"], item)
        return list(unique.values())

    # ------------------------------------------------------------------ connections

    def edit_mode(self, file_id: str) -> bool:
        return file_id in self._edit

    def set_edit(self, file_id: str, edit: bool) -> None:
        if edit:
            self._read_only_access(file_id)
        self.path(file_id)
        with self._lock:
            (self._edit.add if edit else self._edit.discard)(file_id)

    @contextmanager
    def connect(self, file_id: str, write: bool = False) -> Iterator[sqlite3.Connection]:
        path = self.path(file_id)
        self._read_only_access(file_id)
        if write and file_id not in self._edit:
            raise DataError("This file is open read-only: switch on Edit to change it")
        if write and file_id not in self._backed_up:
            self.backup(file_id)
        uri = path.as_uri() + ("?mode=rw" if write else "?mode=ro")
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=3, isolation_level=None, check_same_thread=False)
        except sqlite3.Error as exc:
            raise DataError(f"Couldn't open {path.name}: {exc}") from exc
        conn.set_authorizer(_authorizer)
        deadline = time.monotonic() + QUERY_SECONDS
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        try:
            yield conn
        except sqlite3.OperationalError as exc:
            raise DataError(self._explain(exc)) from exc
        except sqlite3.DatabaseError as exc:
            raise DataError(self._explain(exc)) from exc
        finally:
            conn.close()

    @staticmethod
    def _explain(exc: sqlite3.Error) -> str:
        text = str(exc)
        if "readonly" in text:
            return "This file is open read-only: switch on Edit to change it"
        if "locked" in text or "busy" in text:
            return "Another program is using this file right now (database is locked). Try again in a moment."
        if "interrupted" in text:
            return f"Stopped: the query ran longer than {QUERY_SECONDS} seconds"
        if "not authorized" in text or "authorization denied" in text:
            return ("ATTACH and VACUUM are not allowed in the SQL box (they can reach other files); "
                    "use the Vacuum button to compact the file")
        return text

    # ------------------------------------------------------------------ schema

    def _objects(self, conn: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master "
                            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type DESC, name").fetchall()
        return [{"type": t, "name": n, "table": tb, "sql": s or ""} for t, n, tb, s in rows]

    def _table(self, conn: sqlite3.Connection, table: str) -> dict[str, Any]:
        """The table or view `table` exactly as the file names it (case-insensitive match)."""
        for obj in self._objects(conn):
            if obj["type"] in ("table", "view") and obj["name"].lower() == str(table).lower():
                return obj
        raise DataError(f"No table or view named {table!r}")

    def _columns(self, conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
        return [{"cid": cid, "name": name, "type": ctype or "", "notnull": bool(notnull), "default": default, "pk": pk}
                for cid, name, ctype, notnull, default, pk in conn.execute(f"PRAGMA table_info({quote(table)})")]

    def _has_rowid(self, conn: sqlite3.Connection, obj: dict[str, Any]) -> bool:
        if obj["type"] != "table":
            return False
        try:
            conn.execute(f"SELECT rowid FROM {quote(obj['name'])} LIMIT 0")
            return True
        except sqlite3.OperationalError:
            return False  # WITHOUT ROWID

    def _access(self, file_id: str, op: str, **request: Any) -> Any:
        try:
            return self.access.call(file_id, self.path(file_id), op, **request)
        except AccessError as exc:
            raise DataError(str(exc)) from exc

    def overview(self, file_id: str) -> dict[str, Any]:
        path = self.path(file_id)
        if self.kind(file_id) == "access":
            tables = self._access(file_id, "tables")["tables"]
            _copy, taken = self.access.snapshot(file_id, path)
            stat = path.stat()
            return {"id": file_id, "name": path.name, "path": str(path), "size": stat.st_size, "modified": stat.st_mtime,
                    "kind": "access", "engine": engine_name(path), "snapshot": taken, "edit": False, "tables": tables,
                    "indexes": 0, "triggers": 0, "pragmas": {}, "backups": [], "sqlite": ""}
        with self.connect(file_id) as conn:
            objects = self._objects(conn)
            tables = []
            for obj in objects:
                if obj["type"] not in ("table", "view"):
                    continue
                try:
                    count = conn.execute(f"SELECT count(*) FROM {quote(obj['name'])}").fetchone()[0]
                except sqlite3.Error:
                    count = None
                tables.append({"name": obj["name"], "type": obj["type"], "rows": count})
            tables.sort(key=lambda t: (t["type"] != "table", t["name"].lower()))  # tables first, then views
            pragmas = {name: conn.execute(f"PRAGMA {name}").fetchone()[0]
                       for name in ("journal_mode", "page_size", "page_count", "user_version", "encoding")}
        stat = path.stat()
        return {"id": file_id, "name": path.name, "path": str(path), "size": stat.st_size, "modified": stat.st_mtime,
                "kind": "sqlite", "sqlite": sqlite3.sqlite_version, "edit": self.edit_mode(file_id), "tables": tables,
                "indexes": sum(1 for o in objects if o["type"] == "index"),
                "triggers": sum(1 for o in objects if o["type"] == "trigger"),
                "pragmas": pragmas, "backups": self.backups(file_id)}

    def structure(self, file_id: str, table: str) -> dict[str, Any]:
        if self.kind(file_id) == "access":
            name = self._access_table(file_id, table)
            info = self._access(file_id, "columns", table=name["name"])
            return {"name": name["name"], "type": name["type"], "sql": "", "columns": info["columns"],
                    "indexes": info["indexes"], "foreign_keys": [], "triggers": [], "rowid": False}
        with self.connect(file_id) as conn:
            obj = self._table(conn, table)
            name = obj["name"]
            indexes = []
            if obj["type"] == "table":
                for _seq, iname, unique, origin, _partial in conn.execute(f"PRAGMA index_list({quote(name)})"):
                    cols = [c[2] for c in conn.execute(f"PRAGMA index_info({quote(iname)})")]
                    indexes.append({"name": iname, "unique": bool(unique), "origin": origin, "columns": cols})
            fks = [{"column": f[3], "table": f[2], "to": f[4], "on_update": f[5], "on_delete": f[6]}
                   for f in conn.execute(f"PRAGMA foreign_key_list({quote(name)})")] if obj["type"] == "table" else []
            triggers = [o["name"] for o in self._objects(conn) if o["type"] == "trigger" and o["table"] == name]
            return {"name": name, "type": obj["type"], "sql": obj["sql"], "columns": self._columns(conn, name),
                    "indexes": indexes, "foreign_keys": fks, "triggers": triggers,
                    "rowid": self._has_rowid(conn, obj)}

    # ------------------------------------------------------------------ rows

    def _key_columns(self, conn: sqlite3.Connection, obj: dict[str, Any], columns: list[dict[str, Any]]) -> list[str] | None:
        """How a row is pointed at: ["rowid"], the primary key (WITHOUT ROWID), or None (views)."""
        if self._has_rowid(conn, obj):
            return ["rowid"]
        pk = [c["name"] for c in sorted(columns, key=lambda c: c["pk"]) if c["pk"]]
        return pk or None

    def _access_table(self, file_id: str, table: str) -> dict[str, Any]:
        for t in self._access(file_id, "tables")["tables"]:
            if t["name"].lower() == str(table).lower():
                return t
        raise DataError(f"No table or query named {table!r}")

    def rows(self, file_id: str, table: str, offset: int = 0, limit: int = PAGE_SIZE, order: str = "",
             desc: bool = False, search: str = "", column: str = "") -> dict[str, Any]:
        limit = max(1, min(int(limit), MAX_PAGE))
        offset = max(0, int(offset))
        if self.kind(file_id) == "access":
            obj = self._access_table(file_id, table)
            page = self._access(file_id, "rows", table=obj["name"], offset=offset, limit=limit, order=order,
                                desc=desc, search=search, column=column)
            return {"table": obj["name"], "type": obj["type"], "columns": page["columns"], "editable": False,
                    "rows": [{"key": None, "cells": r} for r in page["rows"]], "total": page["total"],
                    "offset": offset, "limit": limit}
        with self.connect(file_id) as conn:
            obj = self._table(conn, table)
            name = obj["name"]
            columns = self._columns(conn, name)
            names = [c["name"] for c in columns]
            key = self._key_columns(conn, obj, columns)
            where, params = "", []
            if search:
                targets = [c for c in names if c.lower() == column.lower()] if column else names
                if column and not targets:
                    raise DataError(f"No column named {column!r}")
                where = " WHERE " + " OR ".join(f"CAST({quote(c)} AS TEXT) LIKE ?" for c in targets)
                params = [f"%{search}%"] * len(targets)
            order_sql = ""
            if order:
                match = [c for c in names if c.lower() == order.lower()] or (["rowid"] if order == "rowid" and key == ["rowid"] else [])
                if not match:
                    raise DataError(f"No column named {order!r}")
                order_sql = f" ORDER BY {quote(match[0]) if match[0] != 'rowid' else 'rowid'} {'DESC' if desc else 'ASC'}"
            elif key == ["rowid"]:
                order_sql = " ORDER BY rowid"
            key_sql = "rowid AS \"__varsha_rowid__\", " if key == ["rowid"] else ""
            total = conn.execute(f"SELECT count(*) FROM {quote(name)}{where}", params).fetchone()[0]
            cursor = conn.execute(f"SELECT {key_sql}* FROM {quote(name)}{where}{order_sql} LIMIT ? OFFSET ?",
                                  [*params, limit, offset])
            out = []
            for row in cursor.fetchall():
                values = list(row)
                if key == ["rowid"]:
                    row_key: dict[str, Any] | None = {"rowid": values.pop(0)}
                elif key:
                    row_key = {"pk": {k: cell(values[names.index(k)]) for k in key}}
                else:
                    row_key = None
                out.append({"key": row_key, "cells": [cell(v) for v in values]})
            return {"table": name, "type": obj["type"], "columns": columns, "editable": key is not None,
                    "rows": out, "total": total, "offset": offset, "limit": limit}

    def _where_key(self, conn: sqlite3.Connection, obj: dict[str, Any], key: Any) -> tuple[str, list[Any]]:
        if not isinstance(key, dict):
            raise DataError("Which row? (missing key)")
        if "rowid" in key:
            if not self._has_rowid(conn, obj):
                raise DataError("This table has no rowid")
            return "rowid = ?", [int(key["rowid"])]
        pk = key.get("pk")
        columns = self._columns(conn, obj["name"])
        wanted = [c["name"] for c in sorted(columns, key=lambda c: c["pk"]) if c["pk"]]
        if not isinstance(pk, dict) or not wanted or sorted(k.lower() for k in pk) != sorted(w.lower() for w in wanted):
            raise DataError("Which row? (the primary key doesn't match)")
        lookup = {k.lower(): v for k, v in pk.items()}
        values = [from_cell(lookup[w.lower()]) for w in wanted]
        return " AND ".join(f"{quote(w)} IS ?" for w in wanted), values

    def get_cell(self, file_id: str, table: str, key: Any, column: str) -> dict[str, Any]:
        with self.connect(file_id) as conn:
            obj = self._table(conn, table)
            col = self._column(conn, obj["name"], column)
            where, params = self._where_key(conn, obj, key)
            row = conn.execute(f"SELECT {quote(col)} FROM {quote(obj['name'])} WHERE {where}", params).fetchone()
            if row is None:
                raise DataError("That row is gone (changed by someone else?)")
            value = row[0]
            if isinstance(value, str):
                return {"t": "text", "v": value}
            return cell(value)

    def _column(self, conn: sqlite3.Connection, table: str, column: str) -> str:
        for c in self._columns(conn, table):
            if c["name"].lower() == str(column).lower():
                return c["name"]
        raise DataError(f"No column named {column!r}")

    def _write(self, file_id: str, work: Any) -> Any:
        with self.connect(file_id, write=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = work(conn)
                conn.execute("COMMIT")
                return result
            except BaseException:
                conn.execute("ROLLBACK")
                raise

    def update_cell(self, file_id: str, table: str, key: Any, column: str, value: Any) -> str:
        new = from_cell(value)

        def work(conn: sqlite3.Connection) -> str:
            obj = self._table(conn, table)
            if obj["type"] != "table":
                raise DataError("Views can't be edited")
            col = self._column(conn, obj["name"], column)
            where, params = self._where_key(conn, obj, key)
            changed = conn.execute(f"UPDATE {quote(obj['name'])} SET {quote(col)} = ? WHERE {where}", [new, *params]).rowcount
            if changed != 1:
                raise DataError("That row is gone (changed by someone else?)" if changed == 0 else "More than one row matched")
            return f"{col} saved"

        return self._write(file_id, work)

    def insert_row(self, file_id: str, table: str, values: Any) -> str:
        if not isinstance(values, dict):
            raise DataError("values must be {column: value}")

        def work(conn: sqlite3.Connection) -> str:
            obj = self._table(conn, table)
            if obj["type"] != "table":
                raise DataError("Views can't be edited")
            cols = [self._column(conn, obj["name"], c) for c in values]
            data = [from_cell(v) for v in values.values()]
            if cols:
                sql = f"INSERT INTO {quote(obj['name'])} ({', '.join(map(quote, cols))}) VALUES ({', '.join('?' * len(cols))})"
            else:
                sql, data = f"INSERT INTO {quote(obj['name'])} DEFAULT VALUES", []
            row = conn.execute(sql, data).lastrowid
            return f"Row added to {obj['name']}" + (f" (rowid {row})" if row else "")

        return self._write(file_id, work)

    def delete_rows(self, file_id: str, table: str, keys: Any) -> str:
        if not isinstance(keys, list) or not keys or len(keys) > MAX_PAGE:
            raise DataError("Choose the rows to delete")

        def work(conn: sqlite3.Connection) -> str:
            obj = self._table(conn, table)
            if obj["type"] != "table":
                raise DataError("Views can't be edited")
            gone = 0
            for key in keys:
                where, params = self._where_key(conn, obj, key)
                gone += conn.execute(f"DELETE FROM {quote(obj['name'])} WHERE {where}", params).rowcount
            return f"{gone} row{'s' if gone != 1 else ''} deleted from {obj['name']}"

        return self._write(file_id, work)

    # ------------------------------------------------------------------ structure changes

    def add_column(self, file_id: str, table: str, name: str, ctype: str, default: Any = None) -> str:
        name = str(name).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", name):
            raise DataError("Column names: letters, digits and _, starting with a letter")
        ctype = str(ctype or "").upper()
        if ctype not in TYPES:
            raise DataError(f"Type must be one of {', '.join(t for t in TYPES if t)}")

        def work(conn: sqlite3.Connection) -> str:
            obj = self._table(conn, table)
            if obj["type"] != "table":
                raise DataError("Columns can only be added to tables")
            sql = f"ALTER TABLE {quote(obj['name'])} ADD COLUMN {quote(name)}" + (f" {ctype}" if ctype else "")
            if default is not None and default != {"t": "null"}:
                value = from_cell(default)
                sql += " DEFAULT " + (str(value) if isinstance(value, (int, float)) else "'" + str(value).replace("'", "''") + "'")
            conn.execute(sql)
            return f"Column {name} added to {obj['name']}"

        return self._write(file_id, work)

    def drop_table(self, file_id: str, table: str) -> str:
        def work(conn: sqlite3.Connection) -> str:
            obj = self._table(conn, table)
            conn.execute(f"DROP {'VIEW' if obj['type'] == 'view' else 'TABLE'} {quote(obj['name'])}")
            return f"{obj['name']} dropped (the backup taken before your first change still has it)"

        return self._write(file_id, work)

    # ------------------------------------------------------------------ SQL

    def run_sql(self, file_id: str, sql: str) -> dict[str, Any]:
        if self.kind(file_id) == "access":
            started = time.monotonic()
            result = self._access(file_id, "query", sql=str(sql or ""))
            return {"columns": result["columns"], "rows": result["rows"], "truncated": result["truncated"],
                    "changed": 0, "statements": 1, "ms": round((time.monotonic() - started) * 1000, 1), "edit": False}
        statements = split_statements(str(sql or ""))
        if not statements:
            raise DataError("Type a SQL statement")
        if any(re.match(r"\s*(VACUUM|ATTACH|DETACH)\b", s, re.IGNORECASE) for s in statements):
            raise DataError("ATTACH and VACUUM are not allowed in the SQL box (they can reach other files); "
                            "use the Vacuum button to compact the file")
        write = self.edit_mode(file_id)
        started = time.monotonic()
        with self.connect(file_id, write=write) as conn:
            if write:
                conn.execute("BEGIN")
            try:
                columns: list[str] = []
                rows: list[list[dict[str, Any]]] = []
                changed = 0
                truncated = False
                for statement in statements:
                    cursor = conn.execute(statement)
                    if cursor.description:
                        columns = [d[0] for d in cursor.description]
                        batch = cursor.fetchmany(SQL_MAX_ROWS + 1)
                        truncated = len(batch) > SQL_MAX_ROWS
                        rows = [[cell(v) for v in r] for r in batch[:SQL_MAX_ROWS]]
                    elif cursor.rowcount > 0:
                        changed += cursor.rowcount
                if write and conn.in_transaction:
                    conn.execute("COMMIT")
            except BaseException:
                if write and conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
        return {"columns": columns, "rows": rows, "truncated": truncated, "changed": changed,
                "statements": len(statements), "ms": round((time.monotonic() - started) * 1000, 1), "edit": write}

    # ------------------------------------------------------------------ maintenance

    def backup_dir(self, file_id: str) -> Path:
        path = self._files.get(file_id)
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem if path else "file")[:40]
        return self.paths.data / "file-backups" / f"{stem}-{file_id[:8]}"

    def backup(self, file_id: str) -> str:
        """A consistent copy (SQLite's backup API, so a WAL file is included)."""
        path = self.path(file_id)
        self._read_only_access(file_id)
        folder = self.backup_dir(file_id)
        folder.mkdir(parents=True, exist_ok=True)
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + f"-{int(now * 1000) % 1000:03d}"
        target = folder / f"{stamp}{path.suffix or '.sqlite'}"
        n = 1
        while target.exists():  # never overwrite an earlier backup
            target = folder / f"{stamp}-{n}{path.suffix or '.sqlite'}"
            n += 1
        try:
            src = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
            try:
                dst = sqlite3.connect(target)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        except sqlite3.Error as exc:
            target.unlink(missing_ok=True)
            raise DataError(f"Backup failed, nothing was changed: {self._explain(exc)}") from exc
        self._backed_up.add(file_id)
        for old in sorted(folder.iterdir())[:-KEEP_BACKUPS]:
            old.unlink(missing_ok=True)
        logger.info("Backed up %s to %s", path, target)
        return f"Backup saved: {target.name}"

    def backups(self, file_id: str) -> list[dict[str, Any]]:
        folder = self.backup_dir(file_id)
        if not folder.is_dir():
            return []
        return [{"name": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime}
                for p in sorted(folder.iterdir(), reverse=True) if p.is_file()]

    def restore(self, file_id: str, name: str) -> str:
        if file_id not in self._edit:
            raise DataError("Switch on Edit to restore a backup")
        source = self.backup_dir(file_id) / Path(str(name)).name
        if not source.is_file() or not is_sqlite(source):
            raise DataError("No such backup")
        path = self.path(file_id)
        self.backup(file_id)  # the current state, so the restore itself can be undone
        try:
            src = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
            try:
                dst = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
            finally:
                src.close()
        except sqlite3.Error as exc:
            raise DataError(f"Restore failed: {self._explain(exc)}") from exc
        return f"{path.name} restored from {source.name} (the state before is kept as a new backup)"

    def integrity(self, file_id: str) -> str:
        if self.kind(file_id) == "access":
            tables = self._access(file_id, "tables")["tables"]
            return f"Opened fine: {len(tables)} tables and queries readable"
        with self.connect(file_id) as conn:
            result = [r[0] for r in conn.execute("PRAGMA integrity_check(20)")]
            fk = conn.execute("PRAGMA foreign_key_check").fetchmany(20)
        if result == ["ok"] and not fk:
            return "Integrity check: ok"
        problems = [r for r in result if r != "ok"] + [f"foreign key: {row}" for row in fk]
        raise DataError("Integrity check found problems:\n" + "\n".join(problems[:20]))

    def vacuum(self, file_id: str) -> str:
        before = self.path(file_id).stat().st_size
        with self.connect(file_id, write=True) as conn:
            conn.set_authorizer(None)  # VACUUM attaches a temporary database internally; this one is ours
            conn.execute("VACUUM")
        after = self.path(file_id).stat().st_size
        return f"Vacuum done: {before // 1024:,} KB -> {after // 1024:,} KB"

    # ------------------------------------------------------------------ export

    def export_sql(self, file_id: str) -> tuple[bytes, str]:
        path = self.path(file_id)
        with self.connect(file_id) as conn:
            text = "\n".join(conn.iterdump()) + "\n"
        return text.encode("utf-8"), f"{path.stem}.sql"

    def export_csv(self, file_id: str, table: str) -> tuple[bytes, str]:
        if self.kind(file_id) == "access":
            obj = self._access_table(file_id, table)
            text = self._access(file_id, "csv", table=obj["name"])["csv"]
            return ("\ufeff" + text).encode("utf-8"), f"{obj['name']}.csv"
        with self.connect(file_id) as conn:
            obj = self._table(conn, table)
            cursor = conn.execute(f"SELECT * FROM {quote(obj['name'])}")
            out = io.StringIO()
            writer = csv.writer(out, lineterminator="\r\n")
            writer.writerow([d[0] for d in cursor.description])
            for row in cursor:
                writer.writerow(["" if v is None else (bytes(v).hex() if isinstance(v, (bytes, memoryview)) else v) for v in row])
        # UTF-8 with BOM, so Excel shows non-English text correctly
        return ("\ufeff" + out.getvalue()).encode("utf-8"), f"{obj['name']}.csv"
