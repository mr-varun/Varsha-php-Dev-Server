"""MDB Rescue: get data back out of damaged Access .mdb files, from the panel.

The work is done by the MDB rescue tool in bin\\mdb-rescue\\ (copied there with
tools\\sync-mdb-rescue.cmd), with its own portable Python. varsha starts rescue_runner.py under that
Python for each job and follows its JSON lines: stage progress, log lines, the job folder. The tool
never changes the original file: it copies it first (SHA-256 checked) and works on the copy.

Like the Data page, the panel never sends paths: files are picked in a native dialog and then
referred to by a 16-hex id. Results go to data\\mdb-rescue-jobs\\job-<time>-<name>\\.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from varsha.paths import AppPaths

logger = logging.getLogger(__name__)

RUNNER = Path(__file__).with_name("rescue_runner.py")
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_RECENT = 10
LOG_LINES = 400
JOB_RE = re.compile(r"job-[\w.-]{1,120}")
# what the user picks, as in the tool's own window ("What do you need?")
PRESETS: dict[str, set[str]] = {
    "recommended": {"repair", "salvage", "raw", "crosscheck"},
    "raw": {"raw"},
    "carve": {"carve", "raw"},
    "diagnose": set(),
}
STAGE_NAMES = {"copy": "Copy + SHA-256", "carve": "Carving", "diagnose": "Diagnose", "repair": "Engine repair",
               "salvage": "Driver copy", "raw": "Raw page read", "crosscheck": "Cross-check",
               "merge": "Merge (best per table)", "export": "Export .sql / .mdb", "report": "Report"}
STAGE_ORDER = list(STAGE_NAMES)


class RescueError(ValueError):
    pass


def find_tool(paths: AppPaths) -> Path | None:
    for folder in sorted(paths.bin.glob("mdb-rescue*"), reverse=True):
        if (folder / "app" / "mdb_rescue.py").is_file():
            return folder
    return None


def tool_python(tool: Path) -> Path | None:
    for candidate in (tool / "runtime" / "python64" / "python.exe", tool / ".venv" / "Scripts" / "python.exe"):
        if candidate.is_file():
            return candidate
    return None


def stages_for(preset: str, sql: bool, mdb: bool) -> list[str]:
    if preset not in PRESETS:
        raise RescueError(f"Unknown choice {preset!r}")
    steps = set(PRESETS[preset])
    if steps & {"salvage", "raw"} and (sql or mdb):
        steps.add("export")
    return sorted(steps, key=STAGE_ORDER.index)


class RescueManager:
    def __init__(self, paths: AppPaths, notify: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.paths = paths
        self.notify = notify or (lambda _data: None)
        self.jobs_dir = paths.data / "mdb-rescue-jobs"
        self._recent_file = paths.data / "rescue-recent.json"
        self._files: dict[str, Path] = {e["id"]: Path(e["path"]) for e in self._load_recent()}
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._run: dict[str, Any] | None = None
        self._last_push = 0.0

    # ------------------------------------------------------------------ the tool

    @property
    def tool(self) -> Path | None:
        return find_tool(self.paths)

    def available(self) -> str:
        """"" when rescues can run, else why not."""
        tool = self.tool
        if tool is None:
            return "The MDB rescue tool isn't in bin\\mdb-rescue\\ (copy it there with tools\\sync-mdb-rescue.cmd)."
        if tool_python(tool) is None:
            return "bin\\mdb-rescue\\runtime\\python64\\ is missing (copy the tool with its runtime folder)."
        if not RUNNER.is_file():
            return "rescue_runner.py is missing from this varsha build"
        return ""

    # ------------------------------------------------------------------ files

    @staticmethod
    def file_id(path: Path) -> str:
        return hashlib.sha256(str(path).lower().encode("utf-8")).hexdigest()[:16]

    def _load_recent(self) -> list[dict[str, str]]:
        try:
            data = json.loads(self._recent_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [e for e in data if isinstance(e, dict) and isinstance(e.get("id"), str) and isinstance(e.get("path"), str)] \
            if isinstance(data, list) else []

    def register(self, raw: str) -> dict[str, Any]:
        """A file the user picked. A damaged file may not look like Access any more, so only
        existence is checked (and varsha's own data folder is refused)."""
        try:
            path = Path(raw).resolve(strict=True)
        except OSError:
            raise RescueError(f"{raw} doesn't exist") from None
        if not path.is_file():
            raise RescueError(f"{path} isn't a file")
        data = self.paths.data.resolve()
        if data in path.parents:
            raise RescueError("Files inside varsha's data folder can't be rescued here")
        file_id = self.file_id(path)
        self._files[file_id] = path
        recent = [e for e in self._load_recent() if e["id"] != file_id]
        recent.insert(0, {"id": file_id, "path": str(path)})
        self._recent_file.parent.mkdir(parents=True, exist_ok=True)
        self._recent_file.write_text(json.dumps(recent[:MAX_RECENT], indent=2), encoding="utf-8")
        return self._describe(file_id, path)

    def _describe(self, file_id: str, path: Path) -> dict[str, Any]:
        try:
            stat = path.stat()
            return {"id": file_id, "name": path.name, "path": str(path), "size": stat.st_size,
                    "modified": stat.st_mtime, "exists": True}
        except OSError:
            return {"id": file_id, "name": path.name, "path": str(path), "exists": False}

    def recent(self) -> list[dict[str, Any]]:
        return [self._describe(e["id"], Path(e["path"])) for e in self._load_recent()]

    def forget(self, file_id: str) -> None:
        recent = [e for e in self._load_recent() if e["id"] != file_id]
        self._recent_file.write_text(json.dumps(recent, indent=2), encoding="utf-8")

    # ------------------------------------------------------------------ running

    def start(self, file_id: str, preset: str, options: dict[str, Any]) -> str:
        problem = self.available()
        if problem:
            raise RescueError(problem)
        path = self._files.get(file_id)
        if path is None or not path.is_file():
            raise RescueError("Pick the file again (it isn't there any more)")
        sql, mdb, csv_out = bool(options.get("sql")), bool(options.get("mdb")), bool(options.get("csv"))
        steps = stages_for(preset, sql, mdb)
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise RescueError("A rescue is already running: wait for it or stop it first")
            tool = self.tool
            assert tool is not None
            python = tool_python(tool)
            assert python is not None
            self.jobs_dir.mkdir(parents=True, exist_ok=True)
            request = {"file": str(path), "out": str(self.jobs_dir), "steps": steps,
                       "password": str(options.get("password") or "") or None, "csv": csv_out, "sql": sql, "mdb": mdb,
                       "deleted": bool(options.get("deleted"))}
            proc = subprocess.Popen([str(python), "-I", "-X", "utf8", str(RUNNER), str(tool)], stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=str(tool),
                                    creationflags=CREATE_NO_WINDOW)
            assert proc.stdin is not None
            proc.stdin.write((json.dumps(request) + "\n").encode("utf-8"))
            proc.stdin.flush()
            self._proc = proc
            shown = {"copy", "diagnose", "report", *steps, *(["merge"] if set(steps) & {"salvage", "raw"} else [])}
            self._run = {"file": path.name, "file_id": file_id, "preset": preset,
                         "steps": sorted(shown, key=STAGE_ORDER.index),  # the order the tool runs them in
                         "status": "running", "started": time.time(), "job": None, "stages": {}, "log": deque(maxlen=LOG_LINES),
                         "stage": "copy", "error": ""}
        threading.Thread(target=self._follow, args=(proc,), name="mdb-rescue", daemon=True).start()
        logger.info("MDB rescue started for %s (%s)", path, ", ".join(steps) or "diagnose only")
        self._push(force=True)
        return f"Rescue of {path.name} started"

    def _follow(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                event = {"type": "log", "line": line}  # a stray print from the tool
            with self._lock:
                run = self._run
                if run is None:
                    continue
                kind = event.get("type")
                if kind == "job":
                    run["job"] = Path(event["dir"]).name
                elif kind == "progress":
                    stage = str(event.get("stage"))
                    run["stage"] = stage
                    run["stages"][stage] = {"done": event.get("done") or 0, "total": event.get("total") or 0,
                                            "note": event.get("note") or ""}
                elif kind == "log":
                    run["log"].append(str(event.get("line", "")))
                elif kind == "done":
                    run["status"] = str(event.get("status") or "completed")
                elif kind == "error":
                    run["status"], run["error"] = "failed", str(event.get("message") or "")
                    run["log"].append("ERROR " + run["error"])
            self._push()
        code = proc.wait()
        with self._lock:
            if self._run is not None and self._run["status"] == "running":
                self._run["status"] = "failed" if code else "completed"
                if code:
                    self._run["error"] = self._run["error"] or f"the rescue tool stopped unexpectedly (exit {code})"
            self._run["finished"] = time.time() if self._run else None
        logger.info("MDB rescue finished: %s", self._run["status"] if self._run else "?")
        self._push(force=True)

    def _push(self, force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_push > 0.4:  # the tool reports often; the page needs ~2 updates a second
            self._last_push = now
            self.notify(self.status())

    def stop(self) -> str:
        with self._lock:
            proc = self._proc
            finished = self._run is not None and self._run["status"] != "running"  # done, process still exiting
            if proc is None or proc.poll() is not None or finished:
                return "No rescue is running"
            try:
                assert proc.stdin is not None
                proc.stdin.write(b"stop\n")
                proc.stdin.flush()
            except OSError:
                pass
            if self._run:
                self._run["log"].append("stopping at the next safe point (a report is still written)")
        self._push(force=True)
        return "Stopping: the tool finishes the current step safely and writes its report"

    def shutdown(self) -> None:
        """varsha quits: ask a running job to stop (closing stdin does that too)."""
        with self._lock:
            proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                assert proc.stdin is not None
                proc.stdin.close()
            except OSError:
                pass

    def status(self) -> dict[str, Any]:
        with self._lock:
            run = dict(self._run) if self._run else None
        if run:
            run["log"] = list(run["log"])[-200:]
            run["stage_names"] = {s: STAGE_NAMES.get(s, s) for s in run["steps"]}
        return {"available": self.available(), "run": run}

    # ------------------------------------------------------------------ results

    def job_dir(self, job: str) -> Path:
        if not JOB_RE.fullmatch(job):
            raise RescueError("Unknown job")
        folder = self.jobs_dir / job
        if not folder.is_dir():
            raise RescueError("That job folder is gone")
        return folder

    def jobs(self) -> list[dict[str, Any]]:
        out = []
        if not self.jobs_dir.is_dir():
            return out
        for folder in sorted(self.jobs_dir.glob("job-*"), key=lambda p: p.stat().st_mtime, reverse=True)[:50]:
            report = self._report(folder)
            totals = report.get("totals") or {}
            out.append({"job": folder.name, "source": Path(str(report.get("source") or "")).name or folder.name,
                        "status": report.get("status") or ("running" if self._is_current(folder) else "unknown"),
                        "started": report.get("started") or "", "tables": totals.get("tables"),
                        "rows_found": totals.get("rows_found"), "rows_recovered": totals.get("rows_recovered"),
                        "percent": totals.get("percent"), "rows_damaged": totals.get("rows_damaged"),
                        "modified": folder.stat().st_mtime})
        return out

    def _is_current(self, folder: Path) -> bool:
        with self._lock:
            return bool(self._run and self._run.get("job") == folder.name and self._run["status"] == "running")

    @staticmethod
    def _report(folder: Path) -> dict[str, Any]:
        try:
            data = json.loads((folder / "report.json").read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def job(self, job: str) -> dict[str, Any]:
        folder = self.job_dir(job)
        report = self._report(folder)
        outputs = {}
        for key, value in (report.get("outputs") or {}).items():
            p = Path(str(value))
            if p.exists() and folder in p.resolve().parents:
                outputs[key] = {"name": p.name, "size": p.stat().st_size if p.is_file() else None, "folder": p.is_dir()}
        try:
            log = (folder / "log.txt").read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
        except OSError:
            log = []
        diagnosis = report.get("diagnosis") or {}
        return {"job": job, "status": report.get("status") or "unknown", "source": report.get("source") or "",
                "started": report.get("started") or "", "totals": report.get("totals") or {},
                "tables": report.get("tables") or [], "outputs": outputs, "log": log,
                "verdict": diagnosis.get("verdict") or "", "warnings": (diagnosis.get("warnings") or [])[:20],
                "has_report": (folder / "report.html").is_file()}

    def output_path(self, job: str, key: str) -> Path:
        folder = self.job_dir(job)
        value = (self._report(folder).get("outputs") or {}).get(key)
        if not value:
            raise RescueError("This job has no such result")
        path = Path(str(value)).resolve()
        if folder.resolve() not in path.parents or not path.exists():
            raise RescueError("That result file is gone")
        return path

    def open(self, job: str, what: str) -> str:
        """Open the job folder or its report in Windows (Explorer / the default browser)."""
        folder = self.job_dir(job)
        target = folder / "report.html" if what == "report" else folder
        if not target.exists():
            raise RescueError("There is no report for this job")
        os.startfile(str(target))  # noqa: S606 - a varsha-owned job folder, opened for the user
        return f"Opened {target.name}"
