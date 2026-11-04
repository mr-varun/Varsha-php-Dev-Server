"""Scheduled jobs: automatic database backups.

The plan lives in config/schedule.json (what to do and when), the history in
data/schedule-state.json (when each job last ran and how it went), so a restart of varsha
never re-runs a job early and never forgets one: a backup that came due while varsha was off,
or while the database was stopped, runs as soon as the database is running again.

    {
      "backups": {
        "mariadb-11.8": {"enabled": true, "every": "daily", "at": "13:00", "keep": 7,
                         "databases": ["shop"], "since": 1762244000}
      }
    }

"every" is hourly, 6h, 12h, daily (at HH:MM) or weekly (at HH:MM on "day" 0=Monday..6=Sunday).
An empty "databases" list means every database on the server.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from varsha.errors import ConfigError, VarshaError
from varsha.services.database import DB_NAME_RE, DatabaseTools
from varsha.services.manager import ServiceManager
from varsha.services.models import ServiceState

logger = logging.getLogger(__name__)

INTERVALS = {"hourly": 3600, "6h": 6 * 3600, "12h": 12 * 3600}
EVERY = ("hourly", "6h", "12h", "daily", "weekly")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MAX_KEEP = 365


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
        when = {"hourly": "every hour", "6h": "every 6 hours", "12h": "every 12 hours",
                "daily": f"daily at {self.at}", "weekly": f"every {DAYS[self.day]} at {self.at}"}[self.every]
        scope = f"{len(self.databases)} database{'s' if len(self.databases) != 1 else ''}" if self.databases else "all databases"
        return f"{when}, {scope}, keep {self.keep}"


def parse_backup_plan(raw: Any, where: str = "schedule.json") -> BackupPlan:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: a backup plan must be an object")
    plan = BackupPlan()
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            raise ConfigError(f'{where}: "enabled" must be true or false')
        plan = replace(plan, enabled=raw["enabled"])
    if "every" in raw:
        if raw["every"] not in EVERY:
            raise ConfigError(f'{where}: "every" must be one of {", ".join(EVERY)}')
        plan = replace(plan, every=raw["every"])
    if "at" in raw:
        at = raw["at"]
        try:
            hh, mm = (int(x) for x in str(at).split(":"))
            if not (0 <= hh < 24 and 0 <= mm < 60):
                raise ValueError
        except ValueError:
            raise ConfigError(f'{where}: "at" must be a time like 13:00, got {at!r}') from None
        plan = replace(plan, at=f"{hh:02d}:{mm:02d}")
    if "day" in raw:
        day = raw["day"]
        if isinstance(day, bool) or not isinstance(day, int) or not 0 <= day <= 6:
            raise ConfigError(f'{where}: "day" must be 0 (Monday) to 6 (Sunday)')
        plan = replace(plan, day=day)
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
    if "since" in raw:
        if isinstance(raw["since"], bool) or not isinstance(raw["since"], (int, float)):
            raise ConfigError(f'{where}: "since" must be a timestamp')
        plan = replace(plan, since=float(raw["since"]))
    return plan


def next_due(plan: BackupPlan, last_run: float | None) -> float | None:
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


@dataclass
class _Store:
    backups: dict[str, BackupPlan] = field(default_factory=dict)


def _atomic_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


class Scheduler:
    """Runs due backups on a background thread (one at a time per database)."""

    def __init__(self, manager: ServiceManager, databases: DatabaseTools, plan_file: Path, state_file: Path,
                 notify: Callable[[bool, str, str, str], None] | None = None, tick: float = 20.0) -> None:
        self.manager = manager
        self.databases = databases
        self.plan_file = plan_file
        self.state_file = state_file
        self.notify = notify or (lambda ok, title, message, sid: None)
        self.tick = tick
        self._lock = threading.RLock()
        self._running: set[str] = set()
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
        backups = {}
        for sid, plan in (raw.get("backups") or {}).items():
            if not sid.startswith("_"):
                backups[sid] = parse_backup_plan(plan, f"{self.plan_file.name} backups.{sid}")
        return _Store(backups=backups)

    def _save_plans(self) -> None:
        if self.load_error and self.plan_file.is_file():
            # Keep the file we couldn't read, instead of silently overwriting it.
            os.replace(self.plan_file, self.plan_file.with_suffix(".json.broken"))
            self.load_error = ""
        data = {"_comment": "Automatic jobs. Edit in the varsha panel (Databases); see varsha/schedule.py for the format.",
                "backups": {sid: {**asdict(p), "databases": list(p.databases)} for sid, p in sorted(self._store.backups.items())}}
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

    # ------------------------------------------------------------------ plans

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
                    "running": service_id in self._running}

    # ------------------------------------------------------------------ running

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        self._stop.wait(5)  # let adopted/autostarted databases come up first
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception:
                logger.exception("scheduler tick failed")
            self._stop.wait(self.tick)

    def run_due(self, now: float | None = None) -> list[str]:
        """Start every backup that is due; returns the service ids started (for tests)."""
        now = time.time() if now is None else now
        started = []
        with self._lock:
            plans = dict(self._store.backups)
        for sid, plan in plans.items():
            if sid not in self.manager.service_ids:
                continue
            key = f"backup:{sid}"
            with self._lock:
                state = self._state.setdefault(key, JobState())
                due = next_due(plan, state.last_run)
                if due is None or due > now or sid in self._running:
                    continue
                if self.manager.status(sid).state is not ServiceState.RUNNING:
                    if not state.waiting:
                        state.waiting = True
                        state.message = "Due, waiting for the database to run"
                        self._save_state()
                    continue
                self._running.add(sid)
            started.append(sid)
            self._run_backup(sid, plan)
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
            self._running.discard(sid)
            self._state[key] = JobState(last_run=time.time(), last_ok=ok, message=message, file=file)
            self._save_state()
        logger.info("Automatic backup of %s: %s", sid, message if ok else f"FAILED {message}")
        self.notify(ok, f"Automatic backup of {name}" + ("" if ok else " failed"), message, sid)

    def run_now(self, sid: str) -> None:
        """Run this database's automatic backup immediately (counts as its last run)."""
        plan = self.backup_plan(sid)
        with self._lock:
            if sid in self._running:
                raise VarshaError("That backup is already running")
            self._running.add(sid)
        self._run_backup(sid, plan)
