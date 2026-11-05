"""MDB Rescue: varsha runs the rescue tool, follows its progress, lists its jobs and results."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from varsha.data import rescue as rescue_mod
from varsha.data.rescue import RescueError, RescueManager, stages_for
from varsha.paths import AppPaths

HERE = Path(__file__).parent
REPO_TOOL = HERE.parent / "bin" / "mdb-rescue"

FAKE_TOOL = r'''
import json, threading, time
from pathlib import Path

def run_pipeline(file, out=None, password=None, csv_out=True, job_hook=None, echo=True, progress_bar=True,
                 steps=None, deleted=False, sql_out=True, mdb_out=True, command="all"):
    class Job: pass
    job = Job()
    job.dir = Path(out) / ("job-20261105-120000-" + Path(file).stem)
    job.dir.mkdir(parents=True)
    job.cancel_event = None
    job.listeners, job.progress_listeners = [], []
    job_hook(job)
    status = "completed"
    for stage in ["copy", "diagnose", *steps, "report"]:
        if job.cancel_event is not None and job.cancel_event.is_set():
            status = "stopped"
            break
        for fn in job.listeners:
            fn("INFO stage " + stage)
        for done in (0, 1):
            for fn in job.progress_listeners:
                fn(stage, done, 1, "working on " + stage)
        time.sleep(0.15 if "slow" in Path(file).stem else 0)
    sql = job.dir / (Path(file).stem + ".sql")
    sql.write_text("CREATE TABLE t (id INT);", encoding="utf-8")
    rec = job.dir / (Path(file).stem + "_recovered.sqlite")
    import sqlite3
    c = sqlite3.connect(rec); c.execute("CREATE TABLE t (id INTEGER)"); c.execute("INSERT INTO t VALUES (1)"); c.commit(); c.close()
    report = {"status": status, "source": str(file), "totals": {"tables": 1, "rows_found": 5, "rows_recovered": 5,
              "percent": 100.0, "rows_damaged": 0}, "tables": [{"table": "t", "rows_found": 5, "rows_recovered": 5,
              "rows_damaged": 0, "source": "raw page read"}], "outputs": {"sql_dump": str(sql), "recovered_sqlite": str(rec)},
              "diagnosis": {"verdict": "fine", "warnings": []}}
    (job.dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (job.dir / "log.txt").write_text("INFO done\n", encoding="utf-8")
    return job, report
'''


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RescueManager:
    root = tmp_path / "varsha"
    paths = AppPaths(root)
    paths.ensure_dirs()
    app = paths.bin / "mdb-rescue" / "app"
    app.mkdir(parents=True)
    (app / "mdb_rescue.py").write_text(FAKE_TOOL, encoding="utf-8")
    monkeypatch.setattr(rescue_mod, "tool_python", lambda tool: Path(sys._base_executable))  # type: ignore[attr-defined]
    return RescueManager(paths, notify=lambda data: None)


def wait_done(mgr: RescueManager, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = mgr.status()["run"]
        if run and run["status"] != "running":
            return run
        time.sleep(0.05)
    raise AssertionError("the rescue never finished")


def test_presets_map_to_the_tools_steps() -> None:
    assert stages_for("recommended", sql=True, mdb=False) == ["repair", "salvage", "raw", "crosscheck", "export"]
    assert stages_for("recommended", sql=False, mdb=False) == ["repair", "salvage", "raw", "crosscheck"]
    assert stages_for("carve", True, True) == ["carve", "raw", "export"]
    assert stages_for("diagnose", True, True) == []
    with pytest.raises(RescueError):
        stages_for("everything", True, True)


def test_a_job_runs_and_its_results_are_listed(manager: RescueManager, tmp_path: Path) -> None:
    damaged = tmp_path / "work" / "bank.mdb"
    damaged.parent.mkdir()
    damaged.write_bytes(b"\x00\x01\x00\x00garbage")  # damaged files needn't look like Access any more
    file_id = manager.register(str(damaged))["id"]
    assert [f["id"] for f in manager.recent()] == [file_id]
    assert "started" in manager.start(file_id, "recommended", {"sql": True})
    run = wait_done(manager)
    assert run["status"] == "completed" and run["job"] == "job-20261105-120000-bank"
    assert run["steps"] == ["copy", "diagnose", "repair", "salvage", "raw", "crosscheck", "merge", "export", "report"]
    assert run["stages"]["crosscheck"] == {"done": 1, "total": 1, "note": "working on crosscheck"}
    assert "INFO stage export" in run["log"]
    [job] = manager.jobs()
    assert (job["source"], job["status"], job["rows_recovered"], job["percent"]) == ("bank.mdb", "completed", 5, 100.0)
    detail = manager.job(job["job"])
    assert set(detail["outputs"]) == {"sql_dump", "recovered_sqlite"} and detail["verdict"] == "fine"
    assert manager.output_path(job["job"], "sql_dump").name == "bank.sql"
    with pytest.raises(RescueError):
        manager.output_path(job["job"], "rebuilt_mdb")
    with pytest.raises(RescueError):
        manager.job("..\\..\\etc")


def test_stop_and_one_job_at_a_time(manager: RescueManager, tmp_path: Path) -> None:
    slow = tmp_path / "slow.mdb"
    slow.write_bytes(b"x")
    file_id = manager.register(str(slow))["id"]
    manager.start(file_id, "recommended", {"sql": True, "mdb": True})
    with pytest.raises(RescueError, match="already running"):
        manager.start(file_id, "raw", {})
    time.sleep(0.3)
    assert "Stopping" in manager.stop()
    assert wait_done(manager)["status"] == "stopped"
    assert manager.stop() == "No rescue is running"


def test_files_and_setup_are_checked(manager: RescueManager, tmp_path: Path) -> None:
    with pytest.raises(RescueError, match="doesn't exist"):
        manager.register(str(tmp_path / "nope.mdb"))
    inside = manager.paths.data / "x.mdb"
    inside.write_bytes(b"x")
    with pytest.raises(RescueError, match="data folder"):
        manager.register(str(inside))
    with pytest.raises(RescueError, match="Pick the file again"):
        manager.start("0" * 16, "recommended", {})
    empty = RescueManager(AppPaths(tmp_path / "empty"))
    assert "isn't in bin" in empty.available()


def test_results_open_on_the_data_page(manager: RescueManager, tmp_path: Path) -> None:
    from varsha.data.sqlite_files import DataFiles
    damaged = tmp_path / "shop.mdb"
    damaged.write_bytes(b"x")
    manager.start(manager.register(str(damaged))["id"], "raw", {})
    wait_done(manager)
    job = manager.jobs()[0]["job"]
    files = DataFiles(manager.paths)
    info = files.register(str(manager.output_path(job, "recovered_sqlite")))  # inside data\mdb-rescue-jobs: allowed
    assert files.overview(info["id"])["tables"][0]["rows"] == 1


@pytest.mark.skipif(not (REPO_TOOL / "app" / "mdb_rescue.py").is_file(), reason="needs the MDB rescue tool in bin")
def test_the_real_tool_rescues_an_access_file(tmp_path: Path) -> None:
    from varsha.data.access_files import find_python32
    py32 = find_python32(AppPaths(HERE.parent))
    if py32 is None:
        pytest.skip("needs a 32-bit Python to build the .mdb fixture")
    mdb = tmp_path / "work" / "bank.mdb"
    mdb.parent.mkdir()
    subprocess.run([str(py32), "-I", str(HERE / "make_access_fixture.py"), str(mdb)], check=True, capture_output=True, timeout=120)
    mdb.with_suffix(".ldb").unlink(missing_ok=True)
    before = mdb.read_bytes()
    root = tmp_path / "varsha"
    paths = AppPaths(root)
    paths.ensure_dirs()
    import _winapi
    _winapi.CreateJunction(str(REPO_TOOL), str(paths.bin / "mdb-rescue"))
    mgr = RescueManager(paths)
    mgr.start(mgr.register(str(mdb))["id"], "recommended", {"sql": True, "mdb": True})
    run = wait_done(mgr, timeout=300)
    assert run["status"] == "completed", run["log"][-5:]
    detail = mgr.job(run["job"])
    assert detail["totals"]["rows_recovered"] == detail["totals"]["rows_found"] == 40
    assert {"recovered_sqlite", "sql_dump", "rebuilt_mdb"} <= set(detail["outputs"])
    assert mdb.read_bytes() == before  # the original is only read
    json.dumps(mgr.status())  # what the panel sends over SSE is plain JSON
