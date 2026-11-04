"""Doctor: checks report problems clearly, never crash the whole run, and fixes are safe."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from varsha.doctor import Doctor, cert_expiry
from varsha.errors import ServiceError
from varsha.paths import AppPaths
from varsha.services.models import ServiceState

REPO = Path(__file__).resolve().parents[1]


def status(sid: str, state: ServiceState, ports: tuple[int, ...] = (), is_database: bool = False, pid: int | None = None) -> Any:
    return SimpleNamespace(id=sid, name=sid.title(), state=state, ports=ports, is_database=is_database, pid=pid, message="")


def make(tmp_path: Path, statuses: list[Any], plans: dict[str, dict[str, Any]] | None = None) -> Doctor:
    paths = AppPaths(tmp_path)
    paths.ensure_dirs()
    manager = SimpleNamespace(statuses=lambda: statuses, service_ids=[s.id for s in statuses], paths=paths)
    scheduler = SimpleNamespace(backup_status=lambda sid: (plans or {}).get(sid, {"enabled": False, "last_ok": None, "message": ""}),
                                load_error="")
    sites = SimpleNamespace(site_infos=lambda: [], last_error="")
    engine = SimpleNamespace(mkcert_exe=tmp_path / "none.exe", ssl_dir=tmp_path / "ssl", php_builds=lambda: [])
    databases = SimpleNamespace(backup_root=tmp_path / "backups")
    return Doctor(paths, manager, engine, sites, scheduler, databases, dns_route=lambda: False)


def test_a_taken_port_is_a_problem_for_a_stopped_service(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        doctor = make(tmp_path, [status("mariadb", ServiceState.STOPPED, (port,)),
                                 status("apache", ServiceState.RUNNING, (port,))])  # running: its own port, fine
        findings = doctor.check_ports()
    assert [f["id"] for f in findings] == [f"port:{port}"]
    assert findings[0]["status"] == "fail" and "Mariadb can't start" in findings[0]["title"]


def test_free_ports_are_ok(tmp_path: Path) -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert make(tmp_path, [status("db", ServiceState.STOPPED, (port,))]).check_ports()[0]["status"] == "ok"


def test_run_sorts_problems_first_and_survives_a_crashing_check(tmp_path: Path) -> None:
    doctor = make(tmp_path, [status("broken", ServiceState.ERROR), status("gone", ServiceState.MISSING)])
    def check_https() -> list[dict[str, Any]]:
        raise RuntimeError("boom")

    doctor.check_https = check_https  # type: ignore[method-assign]
    result = doctor.run()
    statuses = [f["status"] for f in result["findings"]]
    assert statuses == sorted(statuses, key=["fail", "warn", "info", "ok"].index)
    assert any(f["id"] == "crash:check_https" and "boom" in f["detail"] for f in result["findings"])
    assert result["counts"]["fail"] >= 2  # the failed and the missing service


def test_backups_and_disk(tmp_path: Path) -> None:
    doctor = make(tmp_path, [status("maria", ServiceState.STOPPED, (1,), is_database=True),
                             status("my", ServiceState.STOPPED, (2,), is_database=True)],
                  plans={"my": {"enabled": True, "last_ok": False, "message": "Access denied"}})
    by_id = {f["id"]: f for f in doctor.check_backups()}
    assert by_id["backup:maria"]["status"] == "info" and by_id["backup:my"]["status"] == "fail"
    assert "Access denied" in by_id["backup:my"]["detail"]
    disk = doctor.check_disk()
    assert disk and all(f["status"] in ("ok", "warn", "fail") for f in disk)


def test_stopping_a_process_is_only_allowed_for_varsha_servers(tmp_path: Path) -> None:
    doctor = make(tmp_path, [])
    with pytest.raises(ServiceError, match="isn't one of varsha's servers"):
        doctor.fix(f"stop-pid:{os.getpid()}")  # this test's own python.exe: never
    with pytest.raises(ServiceError, match="gone"):
        doctor.fix("stop-pid:999999999")
    with pytest.raises(ValueError):
        doctor.fix("format-c")
    assert doctor.fix("renew-certs") == "No certificate needed renewing"


def test_leftover_server_processes_are_found(tmp_path: Path) -> None:
    # A copy of python.exe named mysqld.exe inside bin\ stands in for a server left by a crash.
    import sys

    real_python = Path(getattr(sys, "_base_executable", sys.executable))
    fake = tmp_path / "bin" / "mysql" / "mysqld.exe"
    fake.parent.mkdir(parents=True)
    shutil.copy2(real_python, fake)
    for dll in real_python.parent.glob("python*.dll"):
        shutil.copy2(dll, fake.parent / dll.name)
    proc = subprocess.Popen([str(fake), "-c", "import time; time.sleep(60)"])
    try:
        doctor = make(tmp_path, [])
        found = [f for f in doctor.check_leftovers() if f["id"] == f"leftover:{proc.pid}"]
        assert found and found[0]["fix"] == f"stop-pid:{proc.pid}"
        assert doctor.fix(f"stop-pid:{proc.pid}").startswith("Stopped the leftover")
        proc.wait(timeout=10)
    finally:
        proc.kill()


@pytest.mark.skipif(not (REPO / "bin" / "mkcert" / "mkcert.exe").is_file(), reason="needs bin\\mkcert")
def test_certificate_expiry_is_read(tmp_path: Path) -> None:
    env = dict(os.environ, CAROOT=str(tmp_path / "ca"))  # a throwaway CA, never the user's
    subprocess.run([str(REPO / "bin" / "mkcert" / "mkcert.exe"), "-cert-file", str(tmp_path / "x.pem"),
                    "-key-file", str(tmp_path / "x-key.pem"), "x.test"], env=env, capture_output=True, check=True)
    until = cert_expiry(tmp_path / "x.pem")
    assert until is not None and until.year >= 2027  # mkcert certificates last about two years
    assert cert_expiry(tmp_path / "missing.pem") is None
