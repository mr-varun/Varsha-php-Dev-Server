"""Import Banks Data: replace bankdb and/or inventory1 on varsha's MySQL 5.6 with dropped .sql files.

The user's own page (top bar "Import Banks Data"): they drop bankdb.sql and/or inventory1.sql
(exactly those names; only the databases whose file was dropped are touched). Then, by itself:

  1  MySQL 5.6 must answer (it is started if it is stopped)
  2  every file is checked before anything changes: not empty, whole (mysqldump's "Dump
     completed", phpMyAdmin's last SET, or for the bank program's own backup a closed last
     statement), of its own database only, with tables, and statement by statement: each
     statement is a known kind, ends with its delimiter and has its quotes closed
  3  the existing database is backed up to data\\backups\\mysql-5.6\\<bank name>_<db>_<time>.sql
     (the bank name from <db>.branchmaster.branameng, e.g. PACS_SAVALAGI_bankdb_20261106-183000.sql)
  4  the database is dropped and the file imported, with a live log and progress
  5  its tables are counted

Ported from the owner's DABASE_IMPORT_SOFTWARE (importer.py), including its handling of the bank
program's own backup ("-- Backup of bankdb"): its text is the raw bytes of latin1 columns, so it
goes in as latin1, unchanged, in one transaction (a commit per row takes hours).
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from varsha.errors import ServiceError
from varsha.paths import AppPaths
from varsha.secrets import client_option_file
from varsha.services import process as proc_utils

logger = logging.getLogger(__name__)

DATABASES = ("bankdb", "inventory1")
SERVICE = "mysql-5.6"
MAX_UPLOAD = 4 * 1024 ** 3
LOG_LINES = 500
RUN_RE = re.compile(r"run-\d{8}-\d{6}(?:-\d+)?")

OWN_BACKUP_START = (b"SET NAMES latin1;\nSET SESSION sql_mode='NO_AUTO_VALUE_ON_ZERO';\n"
                    b"SET FOREIGN_KEY_CHECKS=0;\nSET UNIQUE_CHECKS=0;\nSET autocommit=0;\n")
OWN_BACKUP_END = b"\nCOMMIT;\n"
STATEMENT_WORDS = (b"CREATE", b"INSERT", b"REPLACE", b"DROP", b"SET", b"LOCK", b"UNLOCK", b"USE", b"ALTER",
                   b"/*!", b"DELETE", b"UPDATE", b"START", b"COMMIT", b"BEGIN", b"TRUNCATE")
ESCAPED = re.compile(rb"\\.")
BACKTICKED = re.compile(rb"`[^`]*`")


class Stop(Exception):
    """A step failed: shown on the page; nothing more is changed."""


def file_name_ok(name: str) -> str | None:
    """The database a dropped file is for, or None (only bankdb.sql / inventory1.sql)."""
    lower = Path(str(name)).name.lower()
    for database in DATABASES:
        if lower == f"{database}.sql":
            return database
    return None


def check_sql(path: Path, database: str, progress: Callable[[int, int], None] = lambda done, total: None) -> dict[str, Any]:
    """Whole, of this database only, and every statement complete. Returns {tables, makes, chooses, own}."""
    total = path.stat().st_size
    if total == 0:
        raise Stop(f"{path.name} is empty")
    with open(path, "rb") as f:
        own = re.match(rb"^-- Backup of (\S+)", f.readline().strip())
    uses: set[str] = set()
    creates: set[str] = set()
    if own:
        uses.add(own.group(1).decode("utf-8", "replace").lower())
    use_line = re.compile(rb"^USE\s+`([^`]+)`", re.I)
    create_line = re.compile(rb"^CREATE DATABASE\b.*?`([^`]+)`", re.I)
    drop_line = re.compile(rb"DROP DATABASE\b.*?`([^`]+)`", re.I)
    tables = 0
    last = b""
    read = shown = 0
    delimiter = b";"
    open_statement = 0          # line number where the current statement started (0: none)
    quotes = 0                  # single quotes so far in the current statement (odd: a string is open)
    with open(path, "rb") as f:
        for number, line in enumerate(f, 1):
            read += len(line)
            if read - shown >= 8 * 1024 * 1024:
                shown = read
                progress(read, total)
            stripped = line.strip()
            if stripped:
                last = stripped  # also comments: mysqldump's last line is "-- Dump completed ..."
            if not stripped:
                if not open_statement:
                    continue
            elif not open_statement:
                if stripped.startswith(b"--") or (stripped.startswith(b"/*") and not stripped.startswith(b"/*!")):
                    if stripped.startswith(b"/*") and not stripped.endswith(b"*/"):
                        pass  # a multi-line comment: rare in dumps; its lines are skipped one by one
                    continue
                if stripped.upper().startswith(b"DELIMITER "):
                    delimiter = stripped[10:].strip() or b";"
                    continue
                if not stripped.upper().startswith(STATEMENT_WORDS) and not stripped.startswith(b"*/"):
                    raise Stop(f"{path.name} line {number}: not an SQL statement ({_preview(stripped)}): "
                               "the file is damaged or not a database dump")
                open_statement = number
                quotes = 0
            if line.startswith(b"CREATE TABLE"):
                tables += 1
            elif line[:3].upper() == b"USE":
                m = use_line.match(line)
                if m:
                    uses.add(m.group(1).decode("utf-8", "replace").lower())
            elif line.startswith((b"CREATE DATABASE", b"/*!", b"DROP DATABASE")) and b"DATABASE" in line[:300].upper():
                m = create_line.match(line) or drop_line.search(line[:300])
                if m:
                    creates.add(m.group(1).decode("utf-8", "replace").lower())
            if open_statement:
                quotes += BACKTICKED.sub(b"", ESCAPED.sub(b"", line)).count(b"'")
                if quotes % 2 == 0 and stripped.endswith(delimiter):
                    open_statement = 0
    progress(total, total)
    if open_statement:
        raise Stop(f"{path.name}: the statement that starts on line {open_statement} never ends "
                   f"({'a quote is not closed' if quotes % 2 else 'no closing ' + delimiter.decode()}): the file is cut off or damaged")
    if own:
        if not last.endswith(b";"):
            raise Stop(f"{path.name} is not whole: it is the bank program's backup and its last line does not end a statement")
    elif b"Dump completed" not in last and not last.startswith(b"/*!40101 SET COLLATION_CONNECTION"):
        raise Stop(f"{path.name} is not whole: its last line is not the end of a dump "
                   "(mysqldump's 'Dump completed' or phpMyAdmin's last SET)")
    if tables == 0:
        raise Stop(f"{path.name} has no tables in it")
    other = (uses | creates) - {database}
    if other:
        raise Stop(f"{path.name} is a dump of another database ({', '.join(sorted(other))})")
    if own:
        return {"tables": tables, "makes": False, "chooses": False, "own": True}
    return {"tables": tables, "makes": bool(creates), "chooses": bool(uses), "own": False}


def _preview(raw: bytes) -> str:
    return raw[:60].decode("utf-8", "replace")


class BankImport:
    def __init__(self, paths: AppPaths, manager: Any, databases: Any,
                 notify: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.paths = paths
        self.manager = manager
        self.databases = databases
        self.notify = notify or (lambda _data: None)
        self.root = paths.data / "bank-import"
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._last_push = 0.0
        self._state: dict[str, Any] = self._idle()

    @staticmethod
    def _idle() -> dict[str, Any]:
        return {"status": "idle", "run": "", "files": [], "log": deque(maxlen=LOG_LINES), "progress": None,
                "backups": {}, "changed": [], "result": {}, "error": "", "started": 0.0, "finished": 0.0}

    # ------------------------------------------------------------------ state

    def status(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
        state["log"] = list(state["log"])[-300:]
        state["service"] = SERVICE
        try:
            state["service_name"] = self.manager.definition(SERVICE).name
        except Exception:  # noqa: BLE001 - services.json without MySQL 5.6
            state["service_name"] = "MySQL 5.6"
        return state

    def _push(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_push > 0.3:
            self._last_push = now
            self.notify(self.status())

    def _log(self, line: str) -> None:
        stamped = time.strftime("%H:%M:%S") + "  " + line
        with self._lock:
            self._state["log"].append(stamped)
        logger.info("Import Banks Data: %s", line)
        self._push()

    def _progress(self, label: str, done: int, total: int) -> None:
        with self._lock:
            self._state["progress"] = {"label": label, "done": done, "total": total}
        self._push()

    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ------------------------------------------------------------------ uploads

    def new_run(self) -> str:
        """A fresh folder for one drop; older runs' uploaded files are removed (the backups stay)."""
        if self.busy():
            raise ServiceError("An import is running: wait for it to finish")
        self.root.mkdir(parents=True, exist_ok=True)
        for old in self.root.glob("run-*"):
            shutil.rmtree(old, ignore_errors=True)
        base = f"run-{time.strftime('%Y%m%d-%H%M%S')}"
        run, k = base, 2
        while (self.root / run).exists():
            run, k = f"{base}-{k}", k + 1
        (self.root / run).mkdir()
        return run

    def save_upload(self, run: str, name: str, stream: BinaryIO, length: int) -> dict[str, Any]:
        if not RUN_RE.fullmatch(run) or not (self.root / run).is_dir():
            raise ValueError("Drop the files again")
        database = file_name_ok(name)
        if database is None:
            raise ValueError(f"{name} isn't bankdb.sql or inventory1.sql")
        if length <= 0 or length > MAX_UPLOAD:
            raise ValueError(f"{name} is empty or too large")
        target = self.root / run / f"{database}.sql"
        remaining = length
        with open(target, "wb") as out:
            while remaining:
                chunk = stream.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                out.write(chunk)
                remaining -= len(chunk)
        if remaining:
            target.unlink(missing_ok=True)
            raise ValueError(f"The upload of {name} was interrupted")
        return {"database": database, "size": length}

    # ------------------------------------------------------------------ the run

    def start(self, run: str) -> str:
        if not RUN_RE.fullmatch(run) or not (self.root / run).is_dir():
            raise ValueError("Drop the files again")
        files = {db: self.root / run / f"{db}.sql" for db in DATABASES if (self.root / run / f"{db}.sql").is_file()}
        if not files:
            raise ValueError("Drop bankdb.sql and/or inventory1.sql")
        if self.busy():
            raise ServiceError("An import is already running")
        with self._lock:
            self._state = self._idle()
            self._state.update(status="running", run=run, files=list(files), started=time.time())
        self._thread = threading.Thread(target=self._job, args=(files,), name="bank-import", daemon=True)
        self._thread.start()
        return "Import of " + " and ".join(files) + " started"

    def _job(self, files: dict[str, Path]) -> None:
        try:
            self._run(files)
            with self._lock:
                self._state["status"] = "done"
        except Stop as exc:
            self._fail(str(exc))
        except Exception as exc:  # noqa: BLE001 - every run ends with a message
            logger.exception("Import Banks Data failed")
            self._fail(f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._state["finished"] = time.time()
                self._state["progress"] = None
            self._push(force=True)

    def _fail(self, message: str) -> None:
        with self._lock:
            self._state["status"] = "failed"
            self._state["error"] = message
            changed = list(self._state["changed"])
        self._log("STOPPED: " + message)
        if changed:
            self._log("ATTENTION: " + " and ".join(changed) + " was already dropped: use Restore below "
                      "to bring the previous data back, or drop the file again")
        else:
            self._log("Nothing was changed")

    def _run(self, files: dict[str, Path]) -> None:
        began = time.time()
        name = self.manager.definition(SERVICE).name
        self._log(f"IMPORT BANKS DATA: {' and '.join(files)} into {name}")
        self._log("[1/5] Checking MySQL")
        status = self.manager.status(SERVICE)
        if status.state.value != "running":
            self._log(f"      {name} is stopped: starting it")
            try:
                self.manager.start(SERVICE)
            except ServiceError as exc:
                raise Stop(f"{name} didn't start: {exc}") from None
        port = self.manager.definition(SERVICE).ports[0]
        version = self._query(port, "SELECT VERSION()")
        self._log(f"      {name} answers on port {port}, version {version}")

        self._log("[2/5] Checking the files (nothing is changed yet)")
        plans = {}
        for database, path in files.items():
            plan = check_sql(path, database, lambda d, t, db=database: self._progress(f"checking {db}.sql", d, t))
            plans[database] = plan
            self._log(f"      {database}.sql: {path.stat().st_size / 1048576:.1f} MB, whole, {plan['tables']} tables, "
                      f"of {database} only, every statement complete"
                      + (" (the bank program's own backup)" if plan["own"] else "") + ": good")

        self._log("[3/5] Backing up the current data")
        existing = set(self._query(port, "SHOW DATABASES").split())
        for database in files:
            if database not in existing:
                self._log(f"      {database} isn't on this server yet: nothing to back up")
                continue
            self._progress(f"backing up {database}", 0, 0)
            bank = self._bank_name(port, database)
            filename = f"{bank}_{database}_{time.strftime('%Y%m%d-%H%M%S')}.sql" if bank else None
            try:
                backup = self.databases.backup(SERVICE, [database], filename=filename)
            except ServiceError as exc:
                raise Stop(f"the backup of {database} failed, so nothing was dropped: {exc}") from None
            with self._lock:
                self._state["backups"][database] = backup.name
            self._log(f"      {database} saved to {backup.name} ({backup.stat().st_size / 1048576:.1f} MB)")

        self._log("[4/5] Replacing the databases")
        counts = {}
        for database, path in files.items():
            plan = plans[database]
            started = time.time()
            self._log(f"      dropping {database} ...")
            self._query(port, f"DROP DATABASE IF EXISTS `{database}`")
            with self._lock:
                self._state["changed"].append(database)
            if not plan["makes"]:
                self._query(port, f"CREATE DATABASE `{database}`")
            self._log(f"      importing {database}.sql ({path.stat().st_size / 1048576:.1f} MB) ...")
            self._import(port, path, database, None if plan["chooses"] else database, plan["own"])
            counts[database] = int(self._query(port, "SELECT COUNT(*) FROM information_schema.tables "
                                                     f"WHERE table_schema = '{database}'") or 0)
            if counts[database] == 0:
                raise Stop(f"after the import {database} has no tables")
            self._log(f"      {database} imported: {counts[database]} tables (the file has {plan['tables']}) "
                      f"in {time.time() - started:.0f} s")
            with self._lock:
                self._state["result"][database] = {"tables": counts[database], "file_tables": plan["tables"]}

        self._log(f"[5/5] DONE in {time.time() - began:.0f} s: "
                  + ", ".join(f"{db} {n} tables" for db, n in counts.items()))

    # ------------------------------------------------------------------ MySQL

    def _bank_name(self, port: int, database: str) -> str:
        """The bank / branch name for the backup's file name: <database>.branchmaster.branameng
        (the English name, e.g. PACS SAVALAGI); "" when the table isn't there."""
        try:
            name = self._query(port, f"SELECT branameng FROM `{database}`.`branchmaster` ORDER BY idno LIMIT 1")
        except Stop:
            return ""
        return re.sub(r"[^A-Za-z0-9._-]+", "_", name.strip()).strip("._")[:60]

    def _client(self) -> Path:
        return self.manager.expand_tool(SERVICE, "client")

    def _query(self, port: int, sql: str) -> str:
        with client_option_file(port, self.manager.credentials.get(SERVICE)) as cnf:
            result = proc_utils.run_quiet([str(self._client()), f"--defaults-extra-file={cnf.as_posix()}",
                                           "-N", "-B", "-e", sql], timeout=600)
        if result.returncode != 0:
            raise Stop("MySQL: " + _clean(result.stderr or result.stdout))
        return result.stdout.strip()

    def _import(self, port: int, path: Path, label: str, into: str | None, own: bool) -> None:
        total = path.stat().st_size
        with client_option_file(port, self.manager.credentials.get(SERVICE)) as cnf:
            cmd = [str(self._client()), f"--defaults-extra-file={cnf.as_posix()}", "--max_allowed_packet=1G",
                   *([into] if into else [])]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                    creationflags=proc_utils.CREATE_NO_WINDOW)
            assert proc.stdin is not None and proc.stderr is not None
            errors: list[bytes] = []
            reader = threading.Thread(target=lambda: errors.append(proc.stderr.read()), daemon=True)
            reader.start()
            sent = shown = 0
            try:
                if own:
                    proc.stdin.write(OWN_BACKUP_START)
                with open(path, "rb") as sql:
                    for chunk in iter(lambda: sql.read(1024 * 1024), b""):
                        proc.stdin.write(chunk)
                        sent += len(chunk)
                        if sent - shown >= 4 * 1024 * 1024:
                            shown = sent
                            self._progress(f"importing {label}", sent, total)
                if own:
                    proc.stdin.write(OWN_BACKUP_END)
            except OSError:
                pass  # mysql.exe ended early: its own words are on stderr
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
            code = proc.wait()
            reader.join(10)
        self._progress(f"importing {label}", total, total)
        if code != 0:
            raise Stop(f"the import of {label} stopped: " + _clean(b"".join(errors).decode("utf-8", "replace")))

    # ------------------------------------------------------------------ restore

    def restore(self, database: str) -> str:
        """Put the backup taken before the last drop back (it re-creates the database itself)."""
        if database not in DATABASES:
            raise ValueError("Unknown database")
        if self.busy():
            raise ServiceError("An import is running: wait for it to finish")
        with self._lock:
            name = self._state["backups"].get(database)
        if not name:
            raise ValueError(f"There is no backup of {database} from the last import")
        backup = self.databases.backup_dir(SERVICE) / name
        if not backup.is_file():
            raise ValueError(f"{name} isn't there any more")
        port = self.manager.definition(SERVICE).ports[0]
        self._log(f"Restoring {database} from {name} ...")
        self._query(port, f"DROP DATABASE IF EXISTS `{database}`")
        self._import(port, backup, database, None, own=False)
        with self._lock:
            if database in self._state["changed"]:
                self._state["changed"].remove(database)
        self._log(f"{database} is back as it was before the import")
        self._push(force=True)
        return f"{database} restored from {name}"


def _clean(text: str) -> str:
    lines = [l for l in str(text).splitlines() if l.strip() and "Using a password" not in l]
    return " ".join(lines)[-700:] or "it ended with an error"
