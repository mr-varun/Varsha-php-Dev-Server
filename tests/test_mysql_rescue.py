"""MySQL Rescue: varsha runs the MySQL rescue tool on a damaged data folder and lists its results."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from varsha.data import rescue as rescue_mod
from varsha.data.mysql_rescue import MysqlRescueManager, looks_like_data
from varsha.data.rescue import RescueError
from varsha.paths import AppPaths

HERE = Path(__file__).parent
REPO = HERE.parent
REPO_TOOL = REPO / "bin" / "mysql-rescue"

FAKE_TOOL = r'''
import json, time
from pathlib import Path

def run_pipeline(folder, mode="recover", out=None, databases=None, extra_server_dirs=(), job_hook=None, echo=True):
    class Job: pass
    job = Job()
    job.dir = Path(out) / ("job-20261106-120000-" + Path(folder).name)
    job.dir.mkdir(parents=True)
    (job.dir / "source").mkdir()
    (job.dir / "source" / "ibdata1").write_bytes(b"x" * 7000)
    (job.dir / "tables" / "shop").mkdir(parents=True)
    (job.dir / "tables" / "shop" / "t.sql").write_bytes(b"y" * 3000)
    job.cancel_event = None
    job.listeners, job.progress_listeners = [], []
    job_hook(job)
    for fn in job.listeners:
        fn("INFO servers " + "|".join(extra_server_dirs) + " mode " + mode)
    status = "completed"
    for stage in ["copy", "diagnose", "start", "dump", "salvage", "combine", "report"]:
        if job.cancel_event is not None and job.cancel_event.is_set():
            status = "stopped"
            break
        for done in (0, 1):
            for fn in job.progress_listeners:
                fn(stage, done, 1, "on " + stage)
        time.sleep(0.15 if "slow" in Path(folder).name else 0)
    sql = job.dir / "shop.sql"
    sql.write_text("CREATE TABLE t (id INT);", encoding="utf-8")
    report = {"status": status, "source": str(folder), "started": "2026-11-06T12:00:00", "original_unchanged": True,
              "diagnosis": {"family": "mariadb", "version": "11.8", "generation": "10.x", "warnings": ["one"]},
              "attempts": [{"server": "MariaDB 11.8.9", "level": 0, "result": "2 tables dumped"}],
              "databases": [{"database": "shop", "ok": 1, "partial": 1, "failed": 0, "rows": 15, "tables": [
                  {"table": "t", "engine": "InnoDB", "rows": 5, "status": "ok", "level": 0, "error": ""},
                  {"table": "u", "engine": "InnoDB", "rows": 10, "status": "partial", "level": 1, "error": "keys 4-9 unreadable"}]}],
              "totals": {"databases": 1, "tables": 2, "tables_ok": 1, "tables_partial": 1, "tables_failed": 0,
                         "rows": 15, "percent": 50.0},
              "outputs": {"sql:shop": str(sql)}}
    (job.dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (job.dir / "report.html").write_text("<p>report</p>", encoding="utf-8")
    (job.dir / "log.txt").write_text("INFO done\n", encoding="utf-8")
    return job, report
'''


def data_folder(parent: Path, name: str = "data") -> Path:
    folder = parent / name
    (folder / "shop").mkdir(parents=True)
    (folder / "ibdata1").write_bytes(b"\0" * 64)
    (folder / "shop" / "t.ibd").write_bytes(b"\0" * 64)
    return folder


@pytest.fixture
def manager(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MysqlRescueManager:
    paths = AppPaths(tmp_path / "varsha")
    paths.ensure_dirs()
    app = paths.bin / "mysql-rescue" / "app"
    app.mkdir(parents=True)
    (app / "mysql_rescue.py").write_text(FAKE_TOOL, encoding="utf-8")
    (paths.bin / "mariadb").mkdir()
    monkeypatch.setattr(rescue_mod, "tool_python", lambda tool: Path(sys._base_executable))  # type: ignore[attr-defined]
    return MysqlRescueManager(paths, notify=lambda data: None)


def wait_done(mgr: MysqlRescueManager, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = mgr.status()["run"]
        if run and run["status"] != "running" and run.get("settled"):
            return run
        time.sleep(0.05)
    raise AssertionError("the rescue never finished")


def test_a_job_runs_and_its_results_are_listed(manager: MysqlRescueManager, tmp_path: Path) -> None:
    folder = data_folder(tmp_path / "old-server")
    info = manager.register(str(folder))
    assert info["exists"] and "size" not in info  # a data folder isn't measured on every listing
    assert "started" in manager.start(info["id"], "recover", {})
    run = wait_done(manager)
    assert run["status"] == "completed" and run["job"] == "job-20261106-120000-data"
    assert run["steps"] == ["copy", "diagnose", "start", "dump", "salvage", "combine", "report"]
    assert any(str(manager.paths.bin / "mariadb") in line and "mode recover" in line for line in run["log"])
    [job] = manager.jobs()
    assert (job["status"], job["tables_ok"], job["tables_partial"], job["rows"]) == ("completed", 1, 1, 15)
    detail = manager.job(job["job"])
    assert detail["detected"] == "mariadb 11.8" and detail["original_unchanged"] is True
    assert [t["status"] for t in detail["databases"][0]["tables"]] == ["ok", "partial"]
    assert set(detail["outputs"]) == {"sql:shop"} and detail["has_report"]
    assert manager.output_path(job["job"], "sql:shop").name == "shop.sql"
    json.dumps(manager.status())


def test_diagnose_shows_only_its_stages_and_stop_works(manager: MysqlRescueManager, tmp_path: Path) -> None:
    folder = data_folder(tmp_path, "slow-data")
    file_id = manager.register(str(folder))["id"]
    manager.start(file_id, "diagnose", {})
    assert manager.status()["run"]["steps"] == ["copy", "diagnose", "report"]
    with pytest.raises(RescueError, match="already running"):
        manager.start(file_id, "recover", {})
    time.sleep(0.3)
    assert "Stopping" in manager.stop()
    assert wait_done(manager)["status"] == "stopped"
    with pytest.raises(RescueError, match="Unknown choice"):
        manager.start(file_id, "everything", {})


def test_folders_are_checked(manager: MysqlRescueManager, tmp_path: Path) -> None:
    with pytest.raises(RescueError, match="doesn't exist"):
        manager.register(str(tmp_path / "nope"))
    (tmp_path / "file.txt").write_text("x", encoding="utf-8")
    with pytest.raises(RescueError, match="isn't a folder"):
        manager.register(str(tmp_path / "file.txt"))
    (tmp_path / "photos").mkdir()
    with pytest.raises(RescueError, match="doesn't look like"):
        manager.register(str(tmp_path / "photos"))
    jobs = manager.paths.data / "mysql-rescue-jobs" / "job-x"
    jobs.mkdir(parents=True)
    with pytest.raises(RescueError, match="job folder"):
        manager.register(str(jobs))
    # the server folder that holds data\ is fine too
    data_folder(tmp_path / "mysql-5.6")
    assert looks_like_data(tmp_path / "mysql-5.6")
    manager.register(str(tmp_path / "mysql-5.6"))


def test_a_running_databases_folder_is_refused(manager: MysqlRescueManager, tmp_path: Path) -> None:
    live = data_folder(manager.paths.data, "mariadb-11.8")
    manager.live_data_dirs = lambda: [("MariaDB 11.8", live)]
    with pytest.raises(RescueError, match="MariaDB 11.8 is running"):
        manager.register(str(live))
    with pytest.raises(RescueError, match="is running"):
        manager.register(str(live / "shop"))  # a database folder inside it too
    manager.live_data_dirs = lambda: []
    file_id = manager.register(str(live))["id"]  # stopped: fine
    manager.live_data_dirs = lambda: [("MariaDB 11.8", live)]
    with pytest.raises(RescueError, match="is running"):
        manager.start(file_id, "recover", {})  # started again since it was picked


def test_clean_keeps_the_sql_and_full_clean_removes_all(manager: MysqlRescueManager, tmp_path: Path) -> None:
    folder = data_folder(tmp_path)
    manager.start(manager.register(str(folder))["id"], "recover", {})
    run = wait_done(manager)
    job_dir = manager.jobs_dir / run["job"]
    assert manager.jobs()[0]["working"] == 7000 + 3000
    assert "freed" in manager.clean_job(run["job"])
    assert sorted(p.name for p in job_dir.iterdir()) == ["log.txt", "report.html", "report.json", "shop.sql"]
    assert "deleted completely" in manager.delete_job(run["job"]) and not job_dir.exists()
    assert (folder / "ibdata1").read_bytes() == b"\0" * 64


# ---------------------------------------------------------------------- the real tool

def mariadb_home() -> Path | None:
    for home in sorted((REPO / "bin" / "mariadb").glob("mariadb-*"), reverse=True):
        if (home / "bin" / "mariadbd.exe").is_file():
            return home
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_mariadb_data(home: Path, datadir: Path) -> None:
    """A real MariaDB data folder with one database (shop: 2 tables, 250 rows), cleanly shut down."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run([str(home / "bin" / "mariadb-install-db.exe"), f"--datadir={datadir}"], check=True,
                   capture_output=True, timeout=180, creationflags=flags)
    port = free_port()
    server = subprocess.Popen([str(home / "bin" / "mariadbd.exe"), "--no-defaults", f"--datadir={datadir.as_posix()}",
                               f"--port={port}", "--bind-address=127.0.0.1", "--skip-grant-tables"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    client = [str(home / "bin" / "mariadb.exe"), "--no-defaults", "--protocol=TCP", "-h127.0.0.1", f"-P{port}", "-uroot"]
    try:
        for _ in range(120):
            if subprocess.run([*client, "-e", "SELECT 1"], capture_output=True, creationflags=flags).returncode == 0:
                break
            time.sleep(0.5)
        rows = ",".join(f"({i},'name {i}')" for i in range(1, 201))
        sql = ("CREATE DATABASE shop; USE shop; CREATE TABLE customers (id INT PRIMARY KEY, name VARCHAR(40)) ENGINE=InnoDB; "
               f"INSERT INTO customers VALUES {rows}; CREATE TABLE notes (id INT PRIMARY KEY, txt TEXT) ENGINE=Aria; "
               + "INSERT INTO notes VALUES " + ",".join(f"({i},'n{i}')" for i in range(1, 51)) + "; "
               "CREATE VIEW first_ten AS SELECT * FROM customers WHERE id <= 10;")
        subprocess.run([*client], input=sql.encode(), check=True, capture_output=True, timeout=60, creationflags=flags)
        subprocess.run([str(home / "bin" / "mariadb-admin.exe"), "--no-defaults", "--protocol=TCP", "-h127.0.0.1",
                        f"-P{port}", "-uroot", "shutdown"], capture_output=True, timeout=60, creationflags=flags)
        server.wait(60)
    finally:
        if server.poll() is None:
            server.kill()


@pytest.mark.skipif(not (REPO_TOOL / "app" / "mysql_rescue.py").is_file() or mariadb_home() is None,
                    reason="needs the MySQL rescue tool and MariaDB in bin")
def test_the_real_tool_rescues_a_mariadb_folder(tmp_path: Path) -> None:
    home = mariadb_home()
    assert home is not None
    datadir = tmp_path / "old" / "data"
    datadir.parent.mkdir()
    build_mariadb_data(home, datadir)
    before = sorted((p.relative_to(datadir), p.stat().st_size) for p in datadir.rglob("*") if p.is_file())
    paths = AppPaths(tmp_path / "varsha")
    paths.ensure_dirs()
    import _winapi
    _winapi.CreateJunction(str(REPO_TOOL), str(paths.bin / "mysql-rescue"))
    _winapi.CreateJunction(str(REPO / "bin" / "mariadb"), str(paths.bin / "mariadb"))
    mgr = MysqlRescueManager(paths)
    assert mgr.available() == ""
    mgr.start(mgr.register(str(datadir))["id"], "recover", {})
    run = wait_done(mgr, timeout=600)
    assert run["status"] == "completed", run["log"][-8:]
    detail = mgr.job(run["job"])
    shop = next(db for db in detail["databases"] if db["database"] == "shop")  # (install-db also makes test)
    rows = {t["table"]: (t["status"], t["rows"]) for t in shop["tables"]}
    assert rows == {"customers": ("ok", 200), "notes": ("ok", 50), "first_ten": ("view", None)}
    sql = mgr.output_path(run["job"], "sql:shop").read_text(encoding="utf-8")
    assert "CREATE TABLE `customers`" in sql and "first_ten" in sql and "CREATE DATABASE" not in sql
    assert detail["original_unchanged"] is True
    after = sorted((p.relative_to(datadir), p.stat().st_size) for p in datadir.rglob("*") if p.is_file())
    assert after == before  # the original is only read
