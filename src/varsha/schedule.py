"""Scheduled jobs: automatic database backups and scheduled tasks (cron).

The plan lives in config/schedule.json (what to do and when), the history in
data/schedule-state.json (when each job last ran and how it went), so a restart of varsha
never re-runs a job early and never forgets one: a job that came due while varsha was off runs
on the next tick; a backup that came due while its database was stopped runs as soon as the
database is running again.

    {
      "backups": {
        "mariadb-11.8": {"enabled": true, "every": "daily", "at": "13:00", "keep": 7,
                         "databases": ["shop"], "since": 1762244000}
      },
      "tasks": {
        "t3f9a1c": {"name": "Laravel scheduler", "site": "admin.test", "where": "project",
                    "command": "php artisan schedule:run", "every": "1m", "enabled": true, ...}
      }
    }

"every" is 1m, 5m, 15m, 30m (tasks only), hourly, 6h, 12h, daily (at HH:MM) or weekly (at HH:MM
on "day" 0=Monday..6=Sunday). An empty "databases" list means every database on the server.

A task runs `cmd.exe /c <command>` in its folder with the site's PHP (plus composer and the
mysql client) first on PATH and PHPRC pointing at varsha's php.ini, exactly like varsha's
terminal. Output goes to logs/tasks/<id>.log. A task never overlaps itself: if the previous run
is still going when it comes due, that run is skipped.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import psutil

from varsha.errors import ConfigError, VarshaError
from varsha.services import process as proc_utils
from varsha.services.database import DB_NAME_RE, DatabaseTools
from varsha.services.manager import ServiceManager
from varsha.services.models import ServiceState

logger = logging.getLogger(__name__)

MINUTE_INTERVALS = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800}
INTERVALS = {**MINUTE_INTERVALS, "hourly": 3600, "6h": 6 * 3600, "12h": 12 * 3600}
EVERY = ("hourly", "6h", "12h", "daily", "weekly")  # backups
TASK_EVERY = ("1m", "5m", "15m", "30m", *EVERY)
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MAX_KEEP = 365
TASK_ID_RE = re.compile(r"t[0-9a-f]{6}")
DOMAIN_RE = re.compile(r"[a-z0-9]([a-z0-9.-]{0,250}[a-z0-9])?")
PHP_RE = re.compile(r"[0-9]{1,2}(\.[0-9]{1,3}){0,2}")
WHERE = ("project", "site", "custom")
TASK_LOG_MAX = 2 * 1024 * 1024
OUTPUT_TAIL = 800


def _describe_timing(every: str, at: str, day: int) -> str:
    return {"1m": "every minute", "5m": "every 5 minutes", "15m": "every 15 minutes", "30m": "every 30 minutes",
            "hourly": "every hour", "6h": "every 6 hours", "12h": "every 12 hours",
            "daily": f"daily at {at}", "weekly": f"every {DAYS[day]} at {at}"}[every]


@dataclass(frozen=True)
class BackupPlan:
    enabled: bool = False
    every: str = "daily"
    at: str = "13:00"
    day: int = 6
    keep: int = 7
    databases: tuple[str, ...] = ()
    since: float = 0.0  # when the plan was saved: the first daily/weekly run is the next slot after it

    def describe(self) -> str:
        if not self.enabled:
            return "off"
        scope = f"{len(self.databases)} database{'s' if len(self.databases) != 1 else ''}" if self.databases else "all databases"
        return f"{_describe_timing(self.every, self.at, self.day)}, {scope}, keep {self.keep}"


@dataclass(frozen=True)
class TaskPlan:
    id: str
    name: str
    command: str
    site: str = ""          # a site domain; "" = not tied to a site
    where: str = "project"  # project: the site's project folder (above public\); site: the www folder; custom: cwd
    cwd: str = ""           # absolute folder, for where == "custom"
    php: str = ""           # version prefix; "" = the site's PHP, else varsha's default
    enabled: bool = True
    every: str = "5m"
    at: str = "00:00"
    day: int = 6
    timeout: int = 600
    since: float = 0.0

    def describe(self) -> str:
        return _describe_timing(self.every, self.at, self.day) if self.enabled else "paused"


def _check_timing(raw: dict[str, Any], where: str, allowed: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise ConfigError(f'{where}: "enabled" must be true or false')
        out["enabled"] = raw["enabled"]
    if "every" in raw:
        if raw["every"] not in allowed:
            raise ConfigError(f'{where}: "every" must be one of {", ".join(allowed)}')
        out["every"] = raw["every"]
    if "at" in raw:
        at = raw["at"]
        try:
            hh, mm = (int(x) for x in str(at).split(":"))
            if not (0 <= hh < 24 and 0 <= mm < 60):
                raise ValueError
        except ValueError:
            raise ConfigError(f'{where}: "at" must be a time like 13:00, got {at!r}') from None
        out["at"] = f"{hh:02d}:{mm:02d}"
    if "day" in raw:
        day = raw["day"]
        if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6:
            raise ConfigError(f'{where}: "day" must be 0 (Monday) to 6 (Sunday)')
        out["day"] = day
    if "since" in raw:
        if isinstance(raw["since"], bool) or not isinstance(raw["since"], (int, float)):
            raise ConfigError(f'{where}: "since" must be a timestamp')
        out["since"] = float(raw["since"])
    return out


def parse_backup_plan(raw: Any, where: str = "schedule.json") -> BackupPlan:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: a backup plan must be an object")
    plan = replace(BackupPlan(), **_check_timing(raw, where, EVERY))
    if "keep" in raw:
        keep = raw["keep"]
        if isinstance(keep, bool) or not isinstance(keep, int) or not 1 <= keep <= MAX_KEEP:
            raise ConfigError(f'{where}: "keep" must be a whole number from 1 to {MAX_KEEP}')
        plan = replace(plan, keep=keep)
    if "databases" in raw:
        dbs = raw["databases"]
        if not isinstance(dbs, list) or not all(isinstance(d, str) and DB_NAME_RE.fullmatch(d) for d in dbs):
            raise ConfigError(f'{where}: "databases" must be a list of database names')
        plan = replace(plan, databases=tuple(dict.fromkeys(dbs)))
    return plan


def parse_task(raw: Any, task_id: str, where: str = "task") -> TaskPlan:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: a task must be an object")
    if not TASK_ID_RE.fullmatch(task_id):
        raise ConfigError(f"{where}: bad task id {task_id!r}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 60:
        raise ConfigError(f"{where}: give the task a name (up to 60 characters)")
    command = raw.get("command")
    if not isinstance(command, str) or not command.strip() or len(command) > 2000 or "\n" in command or "\r" in command:
        raise ConfigError(f"{where}: the command must be one line (up to 2000 characters)")
    fields: dict[str, Any] = {"id": task_id, "name": name.strip(), "command": command.strip()}
    site = raw.get("site") or ""
    if not isinstance(site, str) or (site and not DOMAIN_RE.fullmatch(site)):
        raise ConfigError(f"{where}: bad site {site!r}")
    fields["site"] = site
    place = raw.get("where", "project" if site else "custom")
    if place not in WHERE:
        raise ConfigError(f'{where}: "where" must be one of {", ".join(WHERE)}')
    fields["where"] = place
    cwd = raw.get("cwd") or ""
    if not isinstance(cwd, str):
        raise ConfigError(f"{where}: the folder must be text")
    if place == "custom" and not (cwd and Path(cwd).is_absolute()):
        raise ConfigError(f"{where}: choose a site, or give a full folder path like D:\\scripts")
    if place != "custom" and not site:
        raise ConfigError(f"{where}: choose a site, or give a full folder path")
    fields["cwd"] = cwd.strip()
    php = raw.get("php") or ""
    if not isinstance(php, str) or (php and not PHP_RE.fullmatch(php)):
        raise ConfigError(f"{where}: bad PHP version {php!r}")
    fields["php"] = php
    if "timeout" in raw:
        timeout = raw["timeout"]
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 10 <= timeout <= 86400:
            raise ConfigError(f"{where}: the time limit must be 10 to 86400 seconds")
        fields["timeout"] = timeout
    return replace(TaskPlan(**fields), **_check_timing(raw, where, TASK_EVERY))


def next_due(plan: BackupPlan | TaskPlan, last_run: float | None) -> float | None:
    """When the plan next wants to run (a timestamp), or None when it's off."""
    if not plan.enabled:
        return None
    if plan.every in INTERVALS:
        return (last_run or 0.0) + INTERVALS[plan.every] if last_run else (plan.since or time.time())
    base = datetime.fromtimestamp(max(last_run or 0.0, plan.since or 0.0) or time.time())
    hh, mm = (int(x) for x in plan.at.split(":"))
    slot = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if plan.every == "daily":
        if slot <= base:
            slot += timedelta(days=1)
    else:  # weekly
        slot += timedelta(days=(plan.day - slot.weekday()) % 7)
        if slot <= base:
            slot += timedelta(days=7)
    return slot.timestamp()


@dataclass
class JobState:
    last_run: float | None = None
    last_ok: bool | None = None
    message: str = ""
    file: str = ""
    waiting: bool = False  # due, but the database isn't running
    duration: float = 0.0
    output: str = ""  # the end of the last task run's output
    exit_code: int | None = None


@dataclass
class _Store:
    backups: dict[str, BackupPlan] = field(default_factory=dict)
    tasks: dict[str, TaskPlan] = field(default_factory=dict)


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


# (cwd, environment, PHP label) for a task: supplied by the panel, which knows sites and PHP builds.
PrepareTask = Callable[[TaskPlan], tuple[Path, dict[str, str], str]]


class Scheduler:
    """Runs due backups and tasks. With background=True each job runs on a worker thread."""

    def __init__(self, manager: ServiceManager, databases: DatabaseTools, plan_file: Path, state_file: Path,
                 notify: Callable[[bool, str, str, str], None] | None = None, tick: float = 20.0,
                 prepare_task: PrepareTask | None = None, logs_dir: Path | None = None,
                 background: bool = False) -> None:
        self.manager = manager
        self.databases = databases
        self.plan_file = plan_file
        self.state_file = state_file
        self.notify = notify or (lambda ok, title, message, sid: None)
        self.tick = tick
        self.prepare_task = prepare_task
        self.logs_dir = logs_dir or (plan_file.parent.parent / "logs")
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="job") if background else None
        self._lock = threading.RLock()
        self._running: set[str] = set()  # job keys: "backup:<sid>", "task:<id>"
        self._procs: dict[str, subprocess.Popen[bytes]] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.load_error = ""
        try:
            self._store = self._load_plans()
        except ConfigError as exc:  # a broken file must not stop varsha; the panel shows the error
            logger.error("%s", exc)
            self.load_error = str(exc)
            self._store = _Store()
        self._state = self._load_state()

    # ------------------------------------------------------------------ files

    def _load_plans(self) -> _Store:
        if not self.plan_file.is_file():
            return _Store()
        try:
            raw = json.loads(self.plan_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{self.plan_file.name} is not valid JSON: {exc.msg}") from exc
        store = _Store()
        for sid, plan in (raw.get("backups") or {}).items():
            if not sid.startswith("_"):
                store.backups[sid] = parse_backup_plan(plan, f"{self.plan_file.name} backups.{sid}")
        for tid, task in (raw.get("tasks") or {}).items():
            if not tid.startswith("_"):
                store.tasks[tid] = parse_task(task, tid, f"{self.plan_file.name} tasks.{tid}")
        return store

    def _save_plans(self) -> None:
        if self.load_error and self.plan_file.is_file():
            # Keep the file we couldn't read, instead of silently overwriting it.
            os.replace(self.plan_file, self.plan_file.with_suffix(".json.broken"))
            self.load_error = ""
        data = {"_comment": "Automatic jobs. Edit in the varsha panel (Databases, Tasks); see varsha/schedule.py for the format.",
                "backups": {sid: {**asdict(p), "databases": list(p.databases)} for sid, p in sorted(self._store.backups.items())},
                "tasks": {tid: {k: v for k, v in asdict(t).items() if k != "id"} for tid, t in self._store.tasks.items()}}
        _atomic_write(self.plan_file, data)

    def _load_state(self) -> dict[str, JobState]:
        try:
            raw = json.loads(self.state_file.read_text(encoding="utf-8"))
            return {key: JobState(**{k: v for k, v in value.items() if k in JobState.__dataclass_fields__})
                    for key, value in raw.items()}
        except (OSError, ValueError, TypeError, AttributeError):
            return {}

    def _save_state(self) -> None:
        _atomic_write(self.state_file, {key: asdict(value) for key, value in sorted(self._state.items())})

    # ------------------------------------------------------------------ backup plans

    def backup_plan(self, service_id: str) -> BackupPlan:
        with self._lock:
            return self._store.backups.get(service_id, BackupPlan())

    def set_backup_plan(self, service_id: str, raw: dict[str, Any]) -> BackupPlan:
        defn = self.manager.definition(service_id)
        if not defn.is_database:
            raise VarshaError(f"{defn.name} is not a database")
        with self._lock:
            old = self._store.backups.get(service_id, BackupPlan())
            plan = parse_backup_plan({**raw, "since": time.time()}, "backup plan")
            if plan.enabled and old.enabled and (plan.every, plan.at, plan.day) == (old.every, old.at, old.day):
                plan = replace(plan, since=old.since)  # same timing: keep the schedule where it was
            self._store.backups[service_id] = plan
            self._save_plans()
        logger.info("Backup plan for %s: %s", service_id, plan.describe())
        return plan

    def backup_status(self, service_id: str) -> dict[str, Any]:
        with self._lock:
            plan = self._store.backups.get(service_id, BackupPlan())
            state = self._state.get(f"backup:{service_id}", JobState())
            due = next_due(plan, state.last_run)
            return {**asdict(plan), "databases": list(plan.databases), "describe": plan.describe(),
                    "last_run": state.last_run, "last_ok": state.last_ok, "message": state.message,
                    "file": state.file, "waiting": state.waiting, "next_due": due,
                    "running": f"backup:{service_id}" in self._running}

    # ------------------------------------------------------------------ tasks

    def task_log(self, task_id: str) -> Path:
        if not TASK_ID_RE.fullmatch(task_id):
            raise KeyError(f"unknown task {task_id}")
        return self.logs_dir / "tasks" / f"{task_id}.log"

    def task(self, task_id: str) -> TaskPlan:
        with self._lock:
            if task_id not in self._store.tasks:
                raise KeyError(f"unknown task {task_id}")
            return self._store.tasks[task_id]

    def save_task(self, raw: dict[str, Any], task_id: str | None = None) -> TaskPlan:
        """Create (task_id None) or update a task."""
        with self._lock:
            old = self._store.tasks.get(task_id) if task_id else None
            if task_id and old is None:
                raise KeyError(f"unknown task {task_id}")
            tid = task_id
            while not tid or (task_id is None and tid in self._store.tasks):
                tid = f"t{secrets.token_hex(3)}"
            plan = parse_task({**raw, "since": time.time()}, tid)
            if old and old.enabled and plan.enabled and (plan.every, plan.at, plan.day) == (old.every, old.at, old.day):
                plan = replace(plan, since=old.since)
            self._store.tasks[tid] = plan
            self._save_plans()
        logger.info("Task %s (%s): %s, %s", tid, plan.name, plan.command, plan.describe())
        return plan

    def delete_task(self, task_id: str) -> None:
        with self._lock:
            self.task(task_id)
            del self._store.tasks[task_id]
            self._state.pop(f"task:{task_id}", None)
            self._save_plans()
            self._save_state()

    def task_statuses(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for plan in self._store.tasks.values():
                state = self._state.get(f"task:{plan.id}", JobState())
                out.append({**asdict(plan), "describe": plan.describe(), "next_due": next_due(plan, state.last_run),
                            "last_run": state.last_run, "last_ok": state.last_ok, "message": state.message,
                            "duration": state.duration, "output": state.output, "exit_code": state.exit_code,
                            "running": f"task:{plan.id}" in self._running})
            return sorted(out, key=lambda t: t["name"].lower())

    # ------------------------------------------------------------------ running

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop scheduling and end task processes that are still running (they never outlive varsha)."""
        self._stop.set()
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            try:
                proc_utils.kill_tree(psutil.Process(proc.pid))
            except psutil.Error:
                pass
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        self._stop.wait(5)  # let adopted/autostarted databases come up first
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception:
                logger.exception("scheduler tick failed")
            self._stop.wait(self.tick)

    def _submit(self, fn: Callable[..., None], *args: Any) -> None:
        if self._pool is not None:
            self._pool.submit(fn, *args)
        else:
            fn(*args)

    def run_due(self, now: float | None = None) -> list[str]:
        """Start every job that is due; returns the job ids started (service ids and task ids)."""
        now = time.time() if now is None else now
        started: list[str] = []
        with self._lock:
            backups = dict(self._store.backups)
            tasks = dict(self._store.tasks)
        for sid, plan in backups.items():
            if sid not in self.manager.service_ids:
                continue
            key = f"backup:{sid}"
            with self._lock:
                state = self._state.setdefault(key, JobState())
                due = next_due(plan, state.last_run)
                if due is None or due > now or key in self._running:
                    continue
                if self.manager.status(sid).state is not ServiceState.RUNNING:
                    if not state.waiting:
                        state.waiting = True
                        state.message = "Due, waiting for the database to run"
                        self._save_state()
                    continue
                self._running.add(key)
            started.append(sid)
            self._submit(self._run_backup, sid, plan)
        for tid, task in tasks.items():
            key = f"task:{tid}"
            with self._lock:
                state = self._state.get(key, JobState())
                due = next_due(task, state.last_run)
                if due is None or due > now or key in self._running:
                    continue
                self._running.add(key)
            started.append(tid)
            self._submit(self._run_task, task, False)
        return started

    def _run_backup(self, sid: str, plan: BackupPlan) -> None:
        name = self.manager.definition(sid).name
        key = f"backup:{sid}"
        try:
            target = self.databases.backup(sid, plan.databases or None, auto=True)
            pruned = self.databases.prune_auto(sid, plan.keep)
            size = target.stat().st_size
            message = f"{target.name} ({size / 1048576:.1f} MB)" + (f", removed {len(pruned)} old" if pruned else "")
            ok, file = True, target.name
        except Exception as exc:  # a failed backup must be reported, never crash the scheduler
            if not isinstance(exc, VarshaError):
                logger.exception("Automatic backup of %s failed", sid)
            message, ok, file = str(exc), False, ""
        with self._lock:
            self._running.discard(key)
            self._state[key] = JobState(last_run=time.time(), last_ok=ok, message=message, file=file)
            self._save_state()
        logger.info("Automatic backup of %s: %s", sid, message if ok else f"FAILED {message}")
        self.notify(ok, f"Automatic backup of {name}" + ("" if ok else " failed"), message, sid)

    def run_now(self, sid: str) -> None:
        """Run this database's automatic backup immediately (counts as its last run)."""
        plan = self.backup_plan(sid)
        key = f"backup:{sid}"
        with self._lock:
            if key in self._running:
                raise VarshaError("That backup is already running")
            self._running.add(key)
        self._run_backup(sid, plan)

    def run_task_now(self, task_id: str) -> None:
        plan = self.task(task_id)
        key = f"task:{task_id}"
        with self._lock:
            if key in self._running:
                raise VarshaError(f"{plan.name} is already running")
            self._running.add(key)
        self._submit(self._run_task, plan, True)

    def _run_task(self, plan: TaskPlan, manual: bool) -> None:
        key = f"task:{plan.id}"
        log = self.task_log(plan.id)
        started = time.time()
        code: int | None = None
        output = ""
        try:
            if self.prepare_task is None:
                raise VarshaError("Tasks can't run here (no task environment)")
            cwd, env, php = self.prepare_task(plan)
            log.parent.mkdir(parents=True, exist_ok=True)
            proc_utils.rotate_log(log, TASK_LOG_MAX)
            with open(log, "ab") as out:
                header = f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} {'manual run' if manual else 'scheduled run'} \u00b7 {cwd} \u00b7 {php}\n> {plan.command}\n"
                out.write(header.encode("utf-8"))
                out.flush()
                offset = out.tell()
                # /d: skip AutoRun macros; /s with the outer quotes: the command line is passed as typed.
                proc = subprocess.Popen(f'cmd.exe /d /s /c "{plan.command}"', cwd=str(cwd), env=env,
                                        stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                        creationflags=proc_utils.CREATE_NO_WINDOW)
                with self._lock:
                    self._procs[key] = proc
                try:
                    code = proc.wait(timeout=plan.timeout)
                    message = f"exit {code}" if code else "done"
                except subprocess.TimeoutExpired:
                    proc_utils.kill_tree(psutil.Process(proc.pid))
                    proc.wait(timeout=10)
                    code = None
                    message = f"stopped after the {plan.timeout} s time limit"
                finally:
                    with self._lock:
                        self._procs.pop(key, None)
                # Always on a line of its own, even when the command's output didn't end with a newline.
                out.write(f"\n=== {message} ({time.time() - started:.1f} s)\n".encode("utf-8"))
            with open(log, "rb") as src:
                src.seek(offset)
                output = src.read().decode("utf-8", errors="replace")
            output = output.rsplit("\n===", 1)[0].strip()[-OUTPUT_TAIL:]
            ok = code == 0
        except Exception as exc:  # a broken task must be reported, never crash the scheduler
            if not isinstance(exc, VarshaError):
                logger.exception("Task %s failed to start", plan.id)
            message, ok = str(exc), False
        duration = time.time() - started
        with self._lock:
            before = self._state.get(key, JobState()).last_ok
            self._running.discard(key)
            self._state[key] = JobState(last_run=time.time(), last_ok=ok, message=message, duration=duration,
                                        output=output, exit_code=code)
            self._save_state()
        logger.info("Task %s (%s): %s in %.1f s", plan.id, plan.name, message, duration)
        # Frequent tasks must not flood the tray: report failures once, recoveries once, manual runs always.
        if manual or (not ok and before is not False) or (ok and before is False):
            last_line = output.splitlines()[-1] if output.strip() else ""
            title = f"Task {plan.name}" + ("" if ok else " failed") + (" (working again)" if ok and before is False and not manual else "")
            self.notify(ok, title, message + (f": {last_line}" if last_line and not ok else ""), f"task-{plan.id}")
