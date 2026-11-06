"""MySQL Rescue: get databases back out of a damaged MySQL / MariaDB data folder, from the panel.

The work is done by the owner's MySQL rescue tool in bin\\mysql-rescue\\ (copied there with
tools\\sync-mysql-rescue.cmd), under its own portable Python, through mysql_rescue_runner.py. The
tool copies the folder first (and checks the original is unchanged at the end); every server start
works on a fresh copy of that. It uses varsha's own MySQL / MariaDB builds in bin\\mysql and
bin\\mariadb, started on a private port or pipe, never the services themselves.

The panel never sends paths: the folder is picked in a native dialog and referred to by a 16-hex
id. A data folder of a varsha database that is running is refused (copying a live data folder gives
a torn copy): stop the service first. Results go to data\\mysql-rescue-jobs\\job-<time>-<name>\\:
one <database>.sql per database (no CREATE DATABASE, so it imports into a NEW database), plus the
report. Clean / Full clean are the same as for MDB Rescue.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from varsha.data.rescue import RescueError, RescueManager
from varsha.paths import AppPaths

MODES = ("recover", "force", "diagnose")
STAGE_NAMES = {"copy": "Copy the data folder", "diagnose": "Diagnose (no server)", "start": "Start a server on the copy",
               "dump": "Dump table by table", "salvage": "Read around damaged pages",
               "combine": "One .sql per database", "report": "Report"}
STAGE_ORDER = list(STAGE_NAMES)
# what a MySQL / MariaDB data folder has (one of them is enough: a damaged one may miss the rest)
DATA_MARKERS = ("ibdata1", "mysql", "mysql.ibd", "ib_logfile0", "#innodb_redo", "aria_log_control")
TABLE_FILES = ("*.frm", "*.ibd", "*.MYD", "*.MAD")


def looks_like_data(folder: Path) -> bool:
    for candidate in (folder, folder / "data"):
        if any((candidate / marker).exists() for marker in DATA_MARKERS):
            return True
        try:
            for sub in candidate.iterdir():
                if sub.is_dir() and any(next(sub.glob(p), None) for p in TABLE_FILES):
                    return True
        except OSError:
            pass
    return False


class MysqlRescueManager(RescueManager):
    TOOL_DIR = "mysql-rescue"
    ENTRY = "mysql_rescue.py"
    SYNC = "sync-mysql-rescue.cmd"
    JOBS = "mysql-rescue-jobs"
    RECENT = "mysql-rescue-recent.json"
    LABEL = "MySQL rescue"
    RUNNER = Path(__file__).with_name("mysql_rescue_runner.py")
    STAGE_NAMES = STAGE_NAMES
    STAGE_ORDER = STAGE_ORDER

    def __init__(self, paths: AppPaths, notify: Callable[[dict[str, Any]], None] | None = None,
                 live_data_dirs: Callable[[], list[tuple[str, Path]]] | None = None) -> None:
        super().__init__(paths, notify)
        self.live_data_dirs = live_data_dirs or (lambda: [])  # [(service name, data dir)] of running databases

    def _check_target(self, path: Path) -> None:
        if not path.is_dir():
            raise RescueError(f"{path} isn't a folder")
        data = self.paths.data.resolve()
        for jobs in (data / "mysql-rescue-jobs", data / "mdb-rescue-jobs"):
            if path == jobs or jobs in path.parents:
                raise RescueError("That's a rescue job folder, not a damaged data folder")
        if path.parent == path:
            raise RescueError("Choose the data folder itself, not a whole drive")
        self._check_not_live(path)
        if not looks_like_data(path):
            raise RescueError(f"{path.name} doesn't look like a MySQL / MariaDB data folder "
                              "(no ibdata1, mysql\\ folder or database folders with table files)")

    def _check_not_live(self, path: Path) -> None:
        for name, live in self.live_data_dirs():
            try:
                live = live.resolve()
            except OSError:
                continue
            if path == live or live in path.parents or path in live.parents:
                raise RescueError(f"{name} is running on this folder: stop it first (a running server's "
                                  "folder can't be copied safely)")

    def _describe(self, file_id: str, path: Path) -> dict[str, Any]:
        info = super()._describe(file_id, path)
        info.pop("size", None)  # a data folder can hold many GB; its size is measured by the tool
        info["exists"] = path.is_dir()
        return info

    def _prepare(self, path: Path, preset: str, options: dict[str, Any]) -> tuple[dict[str, Any], list[str], str]:
        if preset not in MODES:
            raise RescueError(f"Unknown choice {preset!r}")
        self._check_not_live(path)  # it may have been started since it was picked
        servers = [str(p) for p in (self.paths.bin / "mysql", self.paths.bin / "mariadb") if p.is_dir()]
        request = {"folder": str(path), "mode": preset, "servers": servers}
        shown = ["copy", "diagnose", "report"] if preset == "diagnose" else STAGE_ORDER
        return request, list(shown), preset

    @staticmethod
    def _source_label(source: str) -> str:
        path = Path(source)  # a folder called data says little: name its server folder too
        return path.parent.name + "\\" + path.name if path.name.lower() == "data" and path.parent.name else path.name

    @staticmethod
    def _summary(totals: dict[str, Any]) -> dict[str, Any]:
        return {key: totals.get(key) for key in ("databases", "tables", "tables_ok", "tables_partial", "tables_failed",
                                                  "rows", "percent")}

    @staticmethod
    def _detail(report: dict[str, Any]) -> dict[str, Any]:
        diagnosis = report.get("diagnosis") or {}
        databases = []
        for db in report.get("databases") or []:
            databases.append({"database": db.get("database"), "ok": db.get("ok"), "partial": db.get("partial", 0),
                              "failed": db.get("failed"), "rows": db.get("rows"),
                              "tables": [{k: t.get(k) for k in ("table", "engine", "rows", "status", "level", "error")}
                                         for t in db.get("tables") or []]})
        detected = " ".join(str(diagnosis.get(k) or "") for k in ("family", "version")).strip()
        return {"databases": databases, "attempts": report.get("attempts") or [],
                "detected": detected, "generation": diagnosis.get("generation") or "",
                "warnings": (diagnosis.get("warnings") or [])[:20],
                "original_unchanged": report.get("original_unchanged")}
