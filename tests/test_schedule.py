"""Automatic database backups: plans, due times, the scheduler and the backup folder setting."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from varsha.errors import ConfigError, ServiceError
from varsha.paths import AppPaths
from varsha.schedule import BackupPlan, Scheduler, next_due, parse_backup_plan
from varsha.services.database import DatabaseTools
from varsha.services.models import ServiceDef, ServiceState
from varsha.settings import Settings, load_settings


def ts(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


# ---------------------------------------------------------------- plans and due times

def test_plan_validation() -> None:
    plan = parse_backup_plan({"enabled": True, "every": "weekly", "at": "9:05", "day": 0, "keep": 3,
                              "databases": ["shop", "shop", "bank_db"]})
    assert (plan.at, plan.databases, plan.keep) == ("09:05", ("shop", "bank_db"), 3)
    assert plan.describe() == "every Monday at 09:05, 2 databases, keep 3"
    for bad in ({"every": "monthly"}, {"at": "25:00"}, {"at": "noon"}, {"keep": 0}, {"keep": True},
                {"day": 7}, {"databases": ["x; DROP"]}, {"databases": "all"}, {"enabled": "yes"}):
        with pytest.raises(ConfigError):
            parse_backup_plan(bad)
    assert BackupPlan().describe() == "off"


def test_daily_runs_at_the_next_slot_after_the_last_run() -> None:
    plan = BackupPlan(enabled=True, every="daily", at="13:00", since=ts("2026-11-04 15:00"))
    assert next_due(plan, None) == ts("2026-11-05 13:00")  # saved after 13:00: first run tomorrow
    assert next_due(plan, ts("2026-11-05 13:00:20")) == ts("2026-11-06 13:00")
    early = BackupPlan(enabled=True, every="daily", at="13:00", since=ts("2026-11-04 09:00"))
    assert next_due(early, None) == ts("2026-11-04 13:00")
    # varsha was off for two days: the backup is overdue now, not "in two days"
    assert next_due(plan, ts("2026-11-05 13:00")) < ts("2026-11-07 10:00")


def test_weekly_and_interval_plans() -> None:
    weekly = BackupPlan(enabled=True, every="weekly", at="20:00", day=6, since=ts("2026-11-04 10:00"))  # a Wednesday
    assert next_due(weekly, None) == ts("2026-11-08 20:00")  # that Sunday
    assert next_due(weekly, ts("2026-11-08 20:00:05")) == ts("2026-11-15 20:00")
    hourly = BackupPlan(enabled=True, every="hourly", since=1000.0)
    assert next_due(hourly, None) == 1000.0  # interval plans run right after they're switched on
    assert next_due(hourly, 5000.0) == 5000.0 + 3600
    assert next_due(BackupPlan(enabled=False), None) is None


# ---------------------------------------------------------------- scheduler

class Status:
    def __init__(self, state: ServiceState) -> None:
        self.state = state


class FakeManager:
    def __init__(self, tmp: Path) -> None:
        self.paths = AppPaths(tmp)
        self.settings = Settings()
        self.states = {"db1": ServiceState.RUNNING}
        self.defs = {"db1": ServiceDef(id="db1", name="Test DB", type="mariadb", exe="x.exe", ports=(3399,),
                                       tools={"client": "{exe_dir}/mariadb.exe", "dump": "{exe_dir}/mariadb-dump.exe"})}

    @property
    def service_ids(self) -> list[str]:
        return list(self.defs)

    def definition(self, sid: str) -> ServiceDef:
        return self.defs[sid]

    def status(self, sid: str) -> Status:
        return Status(self.states[sid])


class FakeTools(DatabaseTools):
    """Writes small files instead of running mariadb-dump."""

    def __init__(self, manager: Any, fail: bool = False) -> None:
        super().__init__(manager)
        self.fail = fail
        self.calls: list[tuple[str, Any, bool]] = []

    def backup(self, service_id: str, databases: Any = None, auto: bool = False) -> Path:
        self.calls.append((service_id, databases, auto))
        if self.fail:
            raise ServiceError("Backup failed: Access denied", service_id)
        folder = self.backup_dir(service_id)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{'auto_' if auto else ''}{service_id}_{len(self.calls):04d}.sql"
        target.write_text("-- dump\n")
        os.utime(target, (time.time() + len(self.calls), time.time() + len(self.calls)))
        return target


def make(tmp_path: Path, fail: bool = False) -> tuple[Scheduler, FakeManager, FakeTools, list[tuple]]:
    manager = FakeManager(tmp_path)
    tools = FakeTools(manager, fail)
    notes: list[tuple] = []
    sched = Scheduler(manager, tools, tmp_path / "config" / "schedule.json", tmp_path / "data" / "schedule-state.json",  # type: ignore[arg-type]
                      notify=lambda *a: notes.append(a))
    return sched, manager, tools, notes


def test_due_backup_runs_prunes_and_reports(tmp_path: Path) -> None:
    sched, _manager, tools, notes = make(tmp_path)
    sched.set_backup_plan("db1", {"enabled": True, "every": "hourly", "keep": 2, "databases": ["shop"]})
    folder = tools.backup_dir("db1")
    folder.mkdir(parents=True)
    (folder / "db1_manual.sql").write_text("-- mine")  # a manual backup: never pruned
    for i in range(3):
        assert sched.run_due(now=time.time() + i * 3601 + 1) == ["db1"]
    assert tools.calls[0] == ("db1", ("shop",), True)
    autos = sorted(p.name for p in folder.glob("auto_*.sql"))
    assert autos == ["auto_db1_0002.sql", "auto_db1_0003.sql"] and (folder / "db1_manual.sql").is_file()
    assert notes[-1][0] is True and "removed 1 old" in notes[-1][2]
    status = sched.backup_status("db1")
    assert status["last_ok"] and status["file"] == "auto_db1_0003.sql" and status["next_due"] > time.time()


def test_waits_for_a_stopped_database_then_catches_up(tmp_path: Path) -> None:
    sched, manager, tools, _notes = make(tmp_path)
    sched.set_backup_plan("db1", {"enabled": True, "every": "6h"})
    manager.states["db1"] = ServiceState.STOPPED
    assert sched.run_due() == []
    assert sched.backup_status("db1")["waiting"] is True and tools.calls == []
    manager.states["db1"] = ServiceState.RUNNING
    assert sched.run_due() == ["db1"]
    assert sched.backup_status("db1")["waiting"] is False


def test_failures_are_reported_and_remembered_across_restarts(tmp_path: Path) -> None:
    sched, _m, _t, notes = make(tmp_path, fail=True)
    sched.set_backup_plan("db1", {"enabled": True, "every": "daily", "at": "00:00"})
    sched.run_now("db1")
    assert notes[-1][0] is False and "Access denied" in notes[-1][2] and notes[-1][3] == "db1"
    again, _m2, tools2, _n2 = make(tmp_path)  # a new varsha session reads the same files
    status = again.backup_status("db1")
    assert status["enabled"] and status["last_ok"] is False and "Access denied" in status["message"]
    assert again.run_due() == [] and tools2.calls == []  # it ran (and failed) today: not due again yet


def test_resaving_the_same_timing_keeps_the_schedule(tmp_path: Path) -> None:
    sched, *_ = make(tmp_path)
    first = sched.set_backup_plan("db1", {"enabled": True, "every": "daily", "at": "13:00", "keep": 7})
    time.sleep(0.01)
    second = sched.set_backup_plan("db1", {"enabled": True, "every": "daily", "at": "13:00", "keep": 30})
    assert second.since == first.since and second.keep == 30
    saved = json.loads((tmp_path / "config" / "schedule.json").read_text())
    assert saved["backups"]["db1"]["keep"] == 30


def test_a_broken_plan_file_is_kept_not_overwritten(tmp_path: Path) -> None:
    plan_file = tmp_path / "config" / "schedule.json"
    plan_file.parent.mkdir(parents=True)
    plan_file.write_text("{ not json")
    sched, *_ = make(tmp_path)
    assert "not valid JSON" in sched.load_error
    sched.set_backup_plan("db1", {"enabled": False})
    assert (tmp_path / "config" / "schedule.json.broken").read_text() == "{ not json"


# ---------------------------------------------------------------- backup folder setting

def test_backup_dir_setting(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"backup_dir": str(tmp_path / "Backups" / "Varsha")}))
    settings = load_settings(settings_file)
    manager = FakeManager(tmp_path / "root")
    manager.settings = settings
    tools = DatabaseTools(manager)  # type: ignore[arg-type]
    assert tools.backup_dir("db1") == tmp_path / "Backups" / "Varsha" / "db1"
    manager.settings = Settings()
    assert tools.backup_dir("db1") == tmp_path / "root" / "data" / "backups" / "db1"
    settings_file.write_text(json.dumps({"backup_dir": "..\\elsewhere"}))
    with pytest.raises(ConfigError):
        load_settings(settings_file)


def test_backup_rejects_bad_database_names(tmp_path: Path) -> None:
    manager = FakeManager(tmp_path)
    tools = DatabaseTools(manager)  # type: ignore[arg-type]
    manager.expand_tool = lambda *_a: Path("mariadb-dump.exe")  # type: ignore[attr-defined]
    with pytest.raises(ServiceError, match="Not a database name"):
        tools.backup("db1", ["ok", "bad name; drop"], auto=True)


def test_today_helper_sanity() -> None:
    # guard for the date maths above: 2026-11-04 is a Wednesday
    assert datetime(2026, 11, 4).weekday() == 2 and (datetime(2026, 11, 8) - timedelta(days=0)).weekday() == 6
