"""Backup, restore and "open in HeidiSQL" for MySQL-compatible services.

Every call needs the database running. Credentials come from the manager's CredentialStore and
reach the tools through a temporary option file (never the command line).
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from varsha.errors import ServiceError
from varsha.secrets import client_option_file
from varsha.services import process as proc_utils
from varsha.services.manager import ServiceManager
from varsha.services.models import ServiceState

logger = logging.getLogger(__name__)

DUMP_OPTIONS = ("--routines", "--events", "--triggers",
                "--single-transaction", "--hex-blob", "--default-character-set=utf8mb4")
AUTO_PREFIX = "auto_"  # automatic (scheduled) backups; only these are ever pruned
SYSTEM_DATABASES = frozenset({"information_schema", "performance_schema", "mysql", "sys"})
DB_NAME_RE = re.compile(r"[A-Za-z0-9_$-]{1,64}")


class DatabaseTools:
    def __init__(self, manager: ServiceManager) -> None:
        self.manager = manager

    @property
    def backup_root(self) -> Path:
        """settings.json "backup_dir" (absolute, or relative to the varsha folder), else data/backups."""
        configured = getattr(self.manager.settings, "backup_dir", "")
        if not configured:
            return self.manager.paths.data / "backups"
        path = Path(configured)
        return path if path.is_absolute() else self.manager.paths.root / path

    def backup_dir(self, service_id: str) -> Path:
        return self.backup_root / service_id

    def _require_running(self, service_id: str) -> int:
        status = self.manager.status(service_id)
        defn = self.manager.definition(service_id)
        if not defn.is_database:
            raise ServiceError(f"{defn.name} is not a database", service_id)
        if status.state is not ServiceState.RUNNING:
            raise ServiceError(f"Start {defn.name} first.", service_id)
        return defn.ports[0]

    def list_databases(self, service_id: str) -> list[str]:
        """User databases on this server (system schemas left out)."""
        port = self._require_running(service_id)
        client = self.manager.expand_tool(service_id, "client")
        with client_option_file(port, self.manager.credentials.get(service_id)) as cnf:
            result = proc_utils.run_quiet([str(client), f"--defaults-extra-file={cnf.as_posix()}", "-N", "-B",
                                           "-e", "SHOW DATABASES"], timeout=60)
        if result.returncode != 0:
            raise ServiceError(f"Couldn't list databases: {_clean(result.stderr or result.stdout)}", service_id)
        return sorted(name for name in (line.strip() for line in result.stdout.splitlines())
                      if name and name.lower() not in SYSTEM_DATABASES)

    def backup(self, service_id: str, databases: Sequence[str] | None = None, auto: bool = False) -> Path:
        """Dump this server to data/backups/<id>/[auto_]<id>_<timestamp>.sql.

        `databases` limits the dump to those databases (default: every database, users included).
        """
        port = self._require_running(service_id)
        dump = self.manager.expand_tool(service_id, "dump")
        folder = self.backup_dir(service_id)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{AUTO_PREFIX if auto else ''}{service_id}_{time.strftime('%Y%m%d-%H%M%S')}.sql"
        if databases:
            bad = [d for d in databases if not DB_NAME_RE.fullmatch(d)]
            if bad:
                raise ServiceError(f"Not a database name: {', '.join(bad)}", service_id)
            selection = ["--databases", *databases]
        else:
            selection = ["--all-databases"]
        with client_option_file(port, self.manager.credentials.get(service_id)) as cnf:
            # --defaults-extra-file must be first; --result-file avoids any console-encoding
            # mangling that redirecting stdout through Windows could cause.
            cmd = [str(dump), f"--defaults-extra-file={cnf.as_posix()}", *DUMP_OPTIONS,
                   f"--result-file={target.as_posix()}", *selection]
            result = proc_utils.run_quiet(cmd, timeout=3600)
        if result.returncode != 0:
            target.unlink(missing_ok=True)
            raise ServiceError(f"Backup failed: {_clean(result.stderr or result.stdout)}", service_id)
        logger.info("Backed up %s to %s (%d bytes)", service_id, target, target.stat().st_size)
        return target

    def prune_auto(self, service_id: str, keep: int) -> list[str]:
        """Delete the oldest automatic backups beyond `keep`. Manual backups are never touched."""
        folder = self.backup_dir(service_id)
        autos = sorted(folder.glob(f"{AUTO_PREFIX}{service_id}_*.sql"), key=lambda p: p.stat().st_mtime, reverse=True)
        removed = []
        for old in autos[max(1, keep):]:
            old.unlink(missing_ok=True)
            removed.append(old.name)
        if removed:
            logger.info("Pruned %d old automatic backups of %s", len(removed), service_id)
        return removed

    def restore(self, service_id: str, sql_file: Path) -> None:
        """Run a .sql file against this server (e.g. a backup made above)."""
        port = self._require_running(service_id)
        client = self.manager.expand_tool(service_id, "client")
        if not sql_file.is_file():
            raise ServiceError(f"{sql_file} not found", service_id)
        with client_option_file(port, self.manager.credentials.get(service_id)) as cnf, \
                open(sql_file, "rb") as source:
            result = subprocess.run(
                [str(client), f"--defaults-extra-file={cnf.as_posix()}", "--default-character-set=utf8mb4"],
                stdin=source, capture_output=True, timeout=None,
                creationflags=proc_utils.CREATE_NO_WINDOW)
        if result.returncode != 0:
            message = (result.stderr or result.stdout).decode("utf-8", errors="replace")
            raise ServiceError(f"Restore failed: {_clean(message)}", service_id)
        logger.info("Restored %s into %s", sql_file, service_id)

    def heidisql_args(self, service_id: str) -> list[str]:
        """Command-line arguments that open HeidiSQL straight on this server."""
        defn = self.manager.definition(service_id)
        args = ["--nettype=0", "--host=127.0.0.1", f"--port={defn.ports[0]}", "--user=root",
                f"--description=varsha {defn.name}"]
        password = self.manager.credentials.get(service_id)
        if password:
            args.append(f"--password={password}")
        return args


def _clean(text: str) -> str:
    """Drop mysql's noisy password warning and keep the message short."""
    lines = [line for line in text.strip().splitlines()
             if "Using a password on the command line" not in line]
    return "\n".join(lines[-5:]) or "unknown error"
