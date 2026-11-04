"""Import databases from another MySQL/MariaDB into one of varsha's database servers.

Two kinds of source:

- A data folder (Laragon, WAMP, XAMPP, an old install...). The original is never opened for
  writing: the system files plus only the chosen databases are copied to a work folder, and a
  server of the same family from varsha's bin\\ is started on that copy, on a spare port, local
  only. MariaDB and MySQL 5.x copies start with --skip-grant-tables; MySQL 8 turns networking off
  with that option, so its copy gets a one-time import user through --init-file instead.
- A running server: host, port, user and password (the password stays in memory only).

Each database is dumped with the source family's own dump tool, imported into the target with
the target's client (skipping, or with mode "replace" dropping, databases that already exist), and
verified table by table: row counts always, CHECKSUM TABLE when both sides are the same family
(different server versions compute checksums differently). The dumps are kept in the target's
backup folder under imports\\ as a safety copy; the copied data folder is deleted afterwards.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets as pysecrets
import shutil
import stat
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from varsha.errors import ServiceError
from varsha.secrets import client_option_file
from varsha.services import process as proc_utils
from varsha.services.database import DB_NAME_RE, SYSTEM_DATABASES, DatabaseTools
from varsha.services.manager import ServiceManager
from varsha.services.models import ServiceState

logger = logging.getLogger(__name__)

FAMILIES = {"mariadb": "MariaDB", "mysql8": "MySQL 8+", "mysql5": "MySQL 5.x"}
TABLE_SUFFIXES = {".frm", ".ibd", ".myd", ".mad", ".csv", ".arz"}
START_TIMEOUT = 120.0
IMPORT_USER = "varsha_import"
SESSION_TTL = 6 * 3600
MAX_RECENT = 8
# Where other local stacks keep their data (only used to offer quick picks; never written to).
KNOWN_FOLDERS = ("C:/laragon/data/*", "D:/laragon/data/*", "C:/xampp/mysql/data", "D:/xampp/mysql/data",
                 "C:/wamp64/bin/mysql/*/data", "C:/wamp64/bin/mariadb/*/data", "C:/wamp/bin/mysql/*/data",
                 "D:/wamp/bin/mysql/*/data", "E:/wamp/bin/mysql/*/data", "E:/Wamp/bin/mysql/*/data",
                 "C:/ProgramData/MySQL/*/Data")


def decode_folder_name(name: str) -> str:
    """MySQL stores special characters in database folder names as @XXXX (e.g. my@002ddb = my-db)."""
    return re.sub(r"@([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), name)


def _is_binlog(name: str) -> bool:
    """Binary and relay log files (binlog.000001, mysql-bin.index, relay-bin.000002, ...)."""
    return bool(re.match(r"(.*-bin|binlog|.*relay-bin|relay-log)\.(\d+|index)$", name))


def quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def family_of_version(version: str) -> str:
    v = version.lower()
    if "mariadb" in v:
        return "mariadb"
    return "mysql5" if v.startswith("5.") else "mysql8"


def inspect_data_folder(path: Path) -> tuple[str, str, list[dict[str, Any]]]:
    """(family, version, databases) read from the folder itself; nothing is started or written."""
    if not path.is_dir():
        raise ServiceError(f"{path} is not a folder")
    if not ((path / "mysql").is_dir() or (path / "ibdata1").is_file() or (path / "mysql.ibd").is_file()):
        raise ServiceError(f"{path} doesn't look like a MySQL/MariaDB data folder (no mysql\\ or ibdata1)")
    version = ""
    for info in ("mariadb_upgrade_info", "mysql_upgrade_info"):
        try:
            version = (path / info).read_text(encoding="utf-8", errors="replace").strip("\x00 \r\n")
            if version:
                break
        except OSError:
            continue
    if (path / "aria_log_control").is_file() or "mariadb" in version.lower():
        family = "mariadb"
    elif (path / "mysql.ibd").is_file():
        family = "mysql8"
    else:
        family = "mysql5"
    databases = []
    for entry in sorted(path.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_dir() or entry.name.startswith("#") or entry.name.lower() in SYSTEM_DATABASES | {"lost+found"}:
            continue
        files = [f for f in entry.iterdir() if f.is_file()]
        tables = {f.stem.split("#P#")[0].split("#p#")[0].lower() for f in files if f.suffix.lower() in TABLE_SUFFIXES}
        databases.append({"name": decode_folder_name(entry.name), "folder": entry.name,
                          "size": sum(f.stat().st_size for f in files), "tables": len(tables)})
    return family, version, databases


def copy_data_folder(source: Path, dest: Path, wanted_folders: set[str]) -> int:
    """Copy what a server needs to start (top-level files, system schemas, #innodb folders) plus the
    wanted database folders. Old logs, pid files and binary/relay logs are left out. Only reads
    `source`. Returns the bytes copied."""
    wanted = {w.lower() for w in wanted_folders}
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    try:
        for entry in source.iterdir():
            name = entry.name.lower()
            if entry.is_file():
                if (name.endswith((".pid", ".err", ".log")) and not name.startswith("ib")) or _is_binlog(name):
                    continue
                shutil.copy2(entry, dest / entry.name)
                copied += entry.stat().st_size
            elif entry.is_dir() and (name in SYSTEM_DATABASES or name.startswith("#") or name in wanted):
                shutil.copytree(entry, dest / entry.name)
                copied += sum(f.stat().st_size for f in (dest / entry.name).rglob("*") if f.is_file())
    except PermissionError as exc:
        raise ServiceError(f"{source} is in use (is a server running on it?). Stop that server, "
                           "or import from it as a running server instead.") from exc
    # Recovered or backed-up folders are often read-only; the server needs to write to its copy.
    for path in [dest, *dest.rglob("*")]:
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE | (stat.S_IEXEC if path.is_dir() else 0))
        except OSError:
            pass
    return copied


@dataclass
class Conn:
    host: str
    port: int
    user: str
    password: str


@dataclass
class Engine:
    service_id: str
    family: str
    version: str
    server: Path
    home: Path
    client: Path
    dump: Path
    admin: Path | None


@dataclass
class ImportSession:
    id: str
    kind: str  # "folder" | "server"
    label: str
    family: str
    version: str
    databases: list[dict[str, Any]]
    path: str = ""
    conn: Conn | None = None
    state: str = "ready"  # ready | running | done | failed
    log: list[str] = field(default_factory=list)
    report: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    target: str = ""
    dump_dir: str = ""
    created: float = field(default_factory=time.time)

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "kind": self.kind, "label": self.label, "family": self.family,
                "family_label": FAMILIES.get(self.family, self.family), "version": self.version,
                "databases": self.databases, "state": self.state, "log": self.log[-200:],
                "report": self.report, "error": self.error, "target": self.target, "dump_dir": self.dump_dir}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class Importer:
    def __init__(self, manager: ServiceManager, databases: DatabaseTools, work_root: Path,
                 notify: Callable[[bool, str, str], None] | None = None) -> None:
        self.manager = manager
        self.databases = databases
        self.work_root = work_root
        self.notify = notify or (lambda ok, title, message: None)
        self._sessions: dict[str, ImportSession] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ engines and sources

    def engines(self) -> dict[str, Engine]:
        """varsha's installed MySQL-compatible servers by family (the first one found wins)."""
        found: dict[str, Engine] = {}
        for sid in self.manager.service_ids:
            defn = self.manager.definition(sid)
            if not defn.is_database or defn.type not in ("mysql", "mariadb"):
                continue
            try:
                ctx = self.manager.context(sid)
                client = self.manager.expand_tool(sid, "client")
                dump = self.manager.expand_tool(sid, "dump")
            except ServiceError:
                continue  # not installed
            version = str(ctx.get("version") or "")
            family = "mariadb" if defn.type == "mariadb" else ("mysql5" if version.startswith("5.") else "mysql8")
            admin = next((p for p in (client.with_name("mariadb-admin.exe"), client.with_name("mysqladmin.exe")) if p.is_file()), None)
            found.setdefault(family, Engine(sid, family, version, Path(str(ctx["exe"])), Path(str(ctx["home"])),
                                            client, dump, admin))
        return found

    def suggestions(self) -> list[dict[str, Any]]:
        """Data folders of other local stacks that exist on this PC (quick picks for the wizard)."""
        own = {str((self.manager.paths.root / d.data_dir).resolve()).lower()
               for d in (self.manager.definition(s) for s in self.manager.service_ids) if d.data_dir}
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for recent in self._recent():  # folders imported from before come first
            try:
                key = str(Path(recent).resolve()).lower()
                if key in own or key in seen:
                    continue
                family, version, dbs = inspect_data_folder(Path(recent))
            except (ServiceError, OSError):
                continue
            seen.add(key)
            out.append({"path": recent, "family": FAMILIES[family], "version": version, "databases": len(dbs), "recent": True})
        for pattern in KNOWN_FOLDERS:
            anchor, _, rest = pattern.partition(":/")
            base = Path(anchor + ":/")
            for folder in (sorted(base.glob(rest)) if base.exists() else []):
                try:
                    key = str(folder.resolve()).lower()
                    if key in own or key in seen:
                        continue
                    seen.add(key)
                    family, version, dbs = inspect_data_folder(folder)
                except (ServiceError, OSError):
                    continue
                out.append({"path": str(folder), "family": FAMILIES[family], "version": version, "databases": len(dbs)})
        return out

    @property
    def _recent_file(self) -> Path:
        return self.work_root.parent / "import-recent.json"

    def _recent(self) -> list[str]:
        try:
            data = json.loads(self._recent_file.read_text(encoding="utf-8"))
            return [p for p in data if isinstance(p, str)][:MAX_RECENT]
        except (OSError, ValueError, TypeError):
            return []

    def _remember(self, path: str) -> None:
        recent = [path] + [p for p in self._recent() if p.lower() != path.lower()]
        try:
            self._recent_file.parent.mkdir(parents=True, exist_ok=True)
            self._recent_file.write_text(json.dumps(recent[:MAX_RECENT], indent=1), encoding="utf-8")
        except OSError:
            pass

    def _new_session(self, **kw: Any) -> ImportSession:
        self._expire()
        session = ImportSession(id=pysecrets.token_hex(6), **kw)
        with self._lock:
            self._sessions[session.id] = session
        return session

    def _expire(self) -> None:
        with self._lock:
            for sid in [s.id for s in self._sessions.values() if s.state != "running" and time.time() - s.created > SESSION_TTL]:
                del self._sessions[sid]

    def session(self, session_id: str) -> ImportSession:
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError("That import is no longer open. Start the wizard again.")
            return self._sessions[session_id]

    def inspect_folder(self, raw_path: str) -> ImportSession:
        path = Path(raw_path.strip().strip('"'))
        if not path.is_absolute():
            raise ServiceError("Give the full path of the data folder, e.g. C:\\laragon\\data\\mariadb-11.8")
        for sid in self.manager.service_ids:
            defn = self.manager.definition(sid)
            if defn.data_dir and (self.manager.paths.root / defn.data_dir).resolve() == path.resolve():
                if self.manager.status(sid).state is not ServiceState.STOPPED:
                    raise ServiceError(f"That is the live data folder of {defn.name}. Stop it first, or import from it as a running server.")
        family, version, dbs = inspect_data_folder(path)
        if family not in self.engines():
            raise ServiceError(f"This is a {FAMILIES[family]} data folder, but varsha has no {FAMILIES[family]} server in bin\\ to read it with.")
        self._remember(str(path))
        return self._new_session(kind="folder", label=str(path), family=family, version=version, databases=dbs, path=str(path))

    def inspect_server(self, host: str, port: int, user: str, password: str) -> ImportSession:
        host = host.strip() or "127.0.0.1"
        if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
            raise ServiceError("That host name isn't valid")
        if not 1 <= int(port) <= 65535:
            raise ServiceError("The port must be 1 to 65535")
        if not re.fullmatch(r"[^\s'\"\\]{1,80}", user or ""):
            raise ServiceError("Give the user name")
        conn = Conn(host, int(port), user, password or "")
        engines = self.engines()
        probe = engines.get("mysql8") or next(iter(engines.values()), None)
        if probe is None:
            raise ServiceError("varsha has no MySQL/MariaDB client in bin\\")
        version = self._sql(probe.client, conn, "SELECT VERSION()")[0][0]
        family = family_of_version(version)
        engine = engines.get(family) or probe
        rows = self._sql(engine.client, conn,
                         "SELECT s.schema_name, COALESCE(SUM(t.data_length + t.index_length), 0), COUNT(t.table_name) "
                         "FROM information_schema.schemata s LEFT JOIN information_schema.tables t "
                         "ON t.table_schema = s.schema_name AND t.table_type = 'BASE TABLE' GROUP BY s.schema_name ORDER BY s.schema_name")
        dbs = [{"name": r[0], "folder": "", "size": int(float(r[1] or 0)), "tables": int(r[2] or 0)}
               for r in rows if r[0].lower() not in SYSTEM_DATABASES]
        return self._new_session(kind="server", label=f"{user}@{host}:{port}", family=family, version=version,
                                 databases=dbs, conn=conn)

    # ------------------------------------------------------------------ running

    def run(self, session_id: str, names: list[str], target_id: str, mode: str) -> ImportSession:
        """Import the chosen databases into target_id. Blocks; call from a worker thread."""
        session = self.session(session_id)
        if session.state == "running":
            raise ServiceError("This import is already running")
        known = {d["name"]: d for d in session.databases}
        chosen = [known[n] for n in dict.fromkeys(names) if n in known]
        if not chosen:
            raise ServiceError("Tick at least one database to import")
        bad = [d["name"] for d in chosen if not DB_NAME_RE.fullmatch(d["name"])]
        if bad:
            raise ServiceError(f"Can't import databases with these names: {', '.join(bad)}")
        if mode not in ("skip", "replace"):
            raise ServiceError("mode must be skip or replace")
        target_def = self.manager.definition(target_id)
        if not target_def.is_database:
            raise ServiceError(f"{target_def.name} is not a database")
        if self.manager.status(target_id).state is not ServiceState.RUNNING:
            raise ServiceError(f"Start {target_def.name} first: the databases are imported into it.")
        session.state, session.target, session.report, session.error, session.log = "running", target_id, [], "", []
        workspace = self.work_root / session.id
        proc: subprocess.Popen[bytes] | None = None
        try:
            engine = self.engines()[session.family] if session.family in self.engines() else None
            if engine is None:
                raise ServiceError(f"No {FAMILIES[session.family]} tools in bin\\")
            if session.kind == "folder":
                conn, proc = self._start_copy(session, engine, chosen, workspace)
            else:
                assert session.conn is not None
                conn = session.conn
            target_port = target_def.ports[0]
            target_password = self.manager.credentials.get(target_id) or ""
            target_conn = Conn("127.0.0.1", target_port, "root", target_password)
            target_client = self.manager.expand_tool(target_id, "client")
            existing = {r[0].lower() for r in self._sql(target_client, target_conn, "SHOW DATABASES")}
            dump_dir = self.databases.backup_dir(target_id) / "imports" / time.strftime("%Y%m%d-%H%M%S")
            dump_dir.mkdir(parents=True, exist_ok=True)
            session.dump_dir = str(dump_dir)
            same_family = session.family == self._family_of_service(target_id)
            for db in chosen:
                self._import_one(session, engine, conn, db["name"], target_client, target_conn, existing, mode, dump_dir, same_family)
            ok = all(r["status"] in ("ok", "skipped") for r in session.report)
            session.state = "done"
            imported = sum(r["status"] == "ok" for r in session.report)
            summary = (f"{imported} imported, {sum(r['status'] == 'skipped' for r in session.report)} skipped"
                       + ("" if ok else f", {sum(r['status'] not in ('ok', 'skipped') for r in session.report)} with problems"))
            self._say(session, f"Finished: {summary}.")
            self.notify(ok, f"Import into {target_def.name}", summary)
        except Exception as exc:
            if not isinstance(exc, ServiceError):
                logger.exception("Import %s failed", session.id)
            session.state, session.error = "failed", str(exc)
            self._say(session, f"FAILED: {exc}")
            self.notify(False, f"Import into {target_def.name} failed", str(exc))
        finally:
            if proc is not None:
                self._stop_copy(session, proc)
            shutil.rmtree(workspace, ignore_errors=True)
        return session

    def _family_of_service(self, sid: str) -> str:
        for family, engine in self.engines().items():
            if engine.service_id == sid:
                return family
        return ""

    def _say(self, session: ImportSession, line: str) -> None:
        session.log.append(f"{time.strftime('%H:%M:%S')}  {line}")
        logger.info("import %s: %s", session.id, line)

    def _start_copy(self, session: ImportSession, engine: Engine, chosen: list[dict[str, Any]],
                    workspace: Path) -> tuple[Conn, subprocess.Popen[bytes]]:
        source = Path(session.path)
        data = workspace / "data"
        self._say(session, f"Copying the system files and {len(chosen)} database(s) from {source} (the original is only read)")
        copied = copy_data_folder(source, data, {d["folder"] for d in chosen})
        self._say(session, f"Copied {copied / 1048576:.1f} MB to a work folder")
        port = _free_port()
        log_path = workspace / "server.log"
        args = [str(engine.server), "--no-defaults", f"--basedir={engine.home.as_posix()}", f"--datadir={data.as_posix()}",
                f"--port={port}", "--bind-address=127.0.0.1", "--console"]
        if session.family == "mysql8":
            password = pysecrets.token_urlsafe(18)
            init = workspace / "init.sql"
            # --init-file runs one statement per line.
            init.write_text(f"CREATE USER IF NOT EXISTS '{IMPORT_USER}'@'127.0.0.1' IDENTIFIED BY '{password}';\n"
                            f"ALTER USER '{IMPORT_USER}'@'127.0.0.1' IDENTIFIED BY '{password}';\n"
                            f"GRANT ALL PRIVILEGES ON *.* TO '{IMPORT_USER}'@'127.0.0.1';\n", encoding="utf-8")
            args += [f"--init-file={init.as_posix()}", "--skip-replica-start", "--skip-log-bin", "--mysqlx=OFF"]
            conn = Conn("127.0.0.1", port, IMPORT_USER, password)
        else:
            args += ["--skip-grant-tables", "--skip-slave-start", "--skip-name-resolve"]
            conn = Conn("127.0.0.1", port, "root", "")
        self._say(session, f"Starting {FAMILIES[session.family]} ({engine.version}) on the copy, port {port}")
        with open(log_path, "wb") as out:
            proc = subprocess.Popen(args, cwd=str(workspace), stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                    creationflags=proc_utils.CREATE_NO_WINDOW)
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-8:]
                raise ServiceError("The server couldn't start on the copied folder:\n" + "\n".join(tail))
            if _port_open(port):
                try:
                    self._sql(engine.client, conn, "SELECT 1")
                    self._say(session, "Server on the copy is ready")
                    return conn, proc
                except ServiceError:
                    pass  # --init-file may still be running
            time.sleep(0.5)
        proc_utils.kill_tree(psutil.Process(proc.pid))
        raise ServiceError(f"The server on the copy didn't answer within {START_TIMEOUT:.0f} s")

    def _stop_copy(self, session: ImportSession, proc: subprocess.Popen[bytes]) -> None:
        try:
            proc_utils.kill_tree(psutil.Process(proc.pid))  # it's a throwaway copy: no graceful stop needed
        except psutil.Error:
            pass
        self._say(session, "Stopped the server on the copy and deleted the copy")

    def _import_one(self, session: ImportSession, engine: Engine, conn: Conn, name: str, target_client: Path,
                    target_conn: Conn, existing: set[str], mode: str, dump_dir: Path, same_family: bool) -> None:
        entry: dict[str, Any] = {"name": name, "status": "", "tables": 0, "rows": 0, "detail": ""}
        session.report.append(entry)
        if name.lower() in existing and mode == "skip":
            entry.update(status="skipped", detail="already exists in the target (choose Replace to overwrite)")
            self._say(session, f"{name}: skipped, it already exists in the target")
            return
        try:
            tables = [r[0] for r in self._sql(engine.client, conn,
                      f"SELECT table_name FROM information_schema.tables WHERE table_schema = '{name}' AND table_type = 'BASE TABLE' ORDER BY table_name")]
            self._say(session, f"{name}: dumping {len(tables)} tables")
            dump_file = dump_dir / f"{name}.sql"
            options = ["--single-transaction", "--routines", "--events", "--triggers", "--hex-blob",
                       "--default-character-set=utf8mb4", f"--result-file={dump_file.as_posix()}"]
            if session.family == "mysql8":
                options += ["--set-gtid-purged=OFF", "--no-tablespaces"]
            with client_option_file(conn.port, conn.password, conn.user, conn.host) as cnf:
                result = proc_utils.run_quiet([str(engine.dump), f"--defaults-extra-file={cnf.as_posix()}", *options,
                                               "--databases", name], timeout=6 * 3600)
            if result.returncode != 0:
                raise ServiceError(f"dump failed: {_clean(result.stderr or result.stdout)}")
            if name.lower() in existing:
                self._sql(target_client, target_conn, f"DROP DATABASE {quote_ident(name)}")
                self._say(session, f"{name}: dropped the old copy in the target (Replace)")
            self._say(session, f"{name}: importing {dump_file.stat().st_size / 1048576:.1f} MB")
            with client_option_file(target_conn.port, target_conn.password, target_conn.user, target_conn.host) as cnf, \
                    open(dump_file, "rb") as source:
                result = subprocess.run([str(target_client), f"--defaults-extra-file={cnf.as_posix()}", "--default-character-set=utf8mb4"],
                                        stdin=source, capture_output=True, timeout=None, creationflags=proc_utils.CREATE_NO_WINDOW)
            if result.returncode != 0:
                raise ServiceError(f"import failed: {_clean(result.stderr.decode('utf-8', 'replace'))}")
            entry["tables"] = len(tables)
            entry.update(self._verify(engine, conn, target_client, target_conn, name, tables, same_family))
            self._say(session, f"{name}: {entry['detail']}")
        except ServiceError as exc:
            entry.update(status="failed", detail=str(exc))
            self._say(session, f"{name}: {exc}")

    def _verify(self, engine: Engine, conn: Conn, target_client: Path, target_conn: Conn, name: str,
                tables: list[str], same_family: bool) -> dict[str, Any]:
        if not tables:
            return {"status": "ok", "detail": "imported (no tables)"}
        db = quote_ident(name)
        count_sql = " UNION ALL ".join(f"SELECT '{_sql_str(t)}', COUNT(*) FROM {db}.{quote_ident(t)}" for t in tables)
        source_rows = {r[0]: int(r[1]) for r in self._sql(engine.client, conn, count_sql)}
        target_rows = {r[0]: int(r[1]) for r in self._sql(target_client, target_conn, count_sql)}
        mismatched = [t for t in tables if source_rows.get(t) != target_rows.get(t)]
        total = sum(source_rows.values())
        if mismatched:
            return {"status": "mismatch", "rows": total,
                    "detail": f"row counts differ in {len(mismatched)} table(s): {', '.join(mismatched[:5])}"}
        if same_family:
            checksum_sql = "CHECKSUM TABLE " + ", ".join(f"{db}.{quote_ident(t)}" for t in tables)
            a = {r[0]: r[1] for r in self._sql(engine.client, conn, checksum_sql)}
            b = {r[0]: r[1] for r in self._sql(target_client, target_conn, checksum_sql)}
            differ = [k for k in a if a.get(k) != b.get(k)]
            if differ:
                return {"status": "mismatch", "rows": total,
                        "detail": f"{len(tables)} tables, {total:,} rows match, but checksums differ in {len(differ)} table(s)"}
            return {"status": "ok", "rows": total, "detail": f"{len(tables)} tables, {total:,} rows, checksums identical"}
        return {"status": "ok", "rows": total, "detail": f"{len(tables)} tables, {total:,} rows, row counts identical"}

    def _sql(self, client: Path, conn: Conn, sql: str) -> list[list[str]]:
        with client_option_file(conn.port, conn.password, conn.user, conn.host) as cnf:
            result = proc_utils.run_quiet([str(client), f"--defaults-extra-file={cnf.as_posix()}", "-N", "-B",
                                           "--default-character-set=utf8mb4", "-e", sql], timeout=600)
        if result.returncode != 0:
            raise ServiceError(_clean(result.stderr or result.stdout))
        return [line.split("\t") for line in result.stdout.splitlines() if line.strip()]


def _sql_str(value: str) -> str:
    """Body of a single-quoted SQL string literal."""
    return value.replace("\\", "\\\\").replace("'", "''")


def _clean(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if "Using a password" not in ln and "insecure passwordless" not in ln]
    return "\n".join(lines[-4:]) or "unknown error"
