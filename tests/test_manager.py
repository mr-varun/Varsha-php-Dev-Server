from __future__ import annotations

import os
import socket
from collections.abc import Callable

import psutil
import pytest

from conftest import fake_def, free_port, wait_for
from varsha.errors import ServiceError
from varsha.services.manager import ServiceManager
from varsha.services.models import ServiceDef, ServiceState, StopMethod, StopSpec
from varsha.services.ports import listening

MakeManager = Callable[..., ServiceManager]


def test_start_and_graceful_stop(make_manager: MakeManager) -> None:
    port = free_port()
    manager = make_manager([fake_def("svc", port)])
    manager.start("svc")
    status = manager.status("svc")
    assert status.state is ServiceState.RUNNING
    assert status.pid and listening([port])

    manager.stop("svc")
    assert manager.status("svc").state is ServiceState.STOPPED
    assert not psutil.pid_exists(status.pid) or not psutil.Process(status.pid).is_running()
    log = status.log_path.read_text(encoding="utf-8", errors="replace")
    assert "fake service listening" in log
    assert "fake service stopping" in log  # it exited through the stop command, not a kill


def test_listener_sees_transitions(make_manager: MakeManager) -> None:
    port = free_port()
    manager = make_manager([fake_def("svc", port)])
    seen: list[ServiceState] = []
    manager.set_listener(lambda sid: seen.append(manager.status(sid).state))
    manager.start("svc")
    manager.stop("svc")
    assert seen == [ServiceState.STARTING, ServiceState.RUNNING, ServiceState.STOPPING,
                    ServiceState.STOPPED]


def test_busy_port_blocks_start(make_manager: MakeManager) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        port = blocker.getsockname()[1]
        manager = make_manager([fake_def("svc", port)])
        with pytest.raises(ServiceError, match=f"PID {os.getpid()}") as info:
            manager.start("svc")
        assert info.value.service_id == "svc"
        assert manager.status("svc").state is ServiceState.ERROR


def test_exit_during_startup_reports_log_tail(make_manager: MakeManager) -> None:
    manager = make_manager([fake_def("svc", free_port(), "--exit-immediately", "3")])
    with pytest.raises(ServiceError, match="exit code 3") as info:
        manager.start("svc")
    assert "failing on purpose" in str(info.value)
    assert manager.status("svc").state is ServiceState.ERROR


def test_kill_after_stop_timeout(make_manager: MakeManager) -> None:
    port = free_port()
    manager = make_manager([fake_def("svc", port, "--ignore-stop")], stop_timeout=1.0)
    manager.start("svc")
    pid = manager.status("svc").pid
    manager.stop("svc")
    assert manager.status("svc").state is ServiceState.STOPPED
    assert wait_for(lambda: not psutil.pid_exists(pid), 5)


def test_terminate_method(make_manager: MakeManager) -> None:
    port = free_port()
    manager = make_manager([fake_def("svc", port, stop=StopSpec(StopMethod.TERMINATE))])
    manager.start("svc")
    manager.stop("svc")
    assert manager.status("svc").state is ServiceState.STOPPED
    assert wait_for(lambda: not listening([port]), 5)


def test_missing_stop_command_kills_immediately(make_manager: MakeManager) -> None:
    stop = StopSpec(StopMethod.COMMAND, ("{exe_dir}/does-not-exist.exe",))
    manager = make_manager([fake_def("svc", free_port(), stop=stop)], stop_timeout=30.0)
    manager.start("svc")
    manager.stop("svc")  # would take 30 s if it waited for the timeout
    assert manager.status("svc").state is ServiceState.STOPPED


def test_crash_is_restarted(make_manager: MakeManager) -> None:
    port = free_port()
    manager = make_manager([fake_def("svc", port)])
    manager.start_monitor()
    manager.start("svc")
    first_pid = manager.status("svc").pid
    psutil.Process(first_pid).kill()

    assert wait_for(lambda: (manager.status("svc").state is ServiceState.RUNNING
                             and manager.status("svc").pid not in (None, first_pid)), 10)
    assert listening([port])


def test_gives_up_after_max_restarts(make_manager: MakeManager) -> None:
    port = free_port()
    manager = make_manager([fake_def("svc", port)], max_auto_restarts=2)
    manager.start_monitor()
    manager.start("svc")
    for _attempt in range(3):
        status = manager.status("svc")
        assert wait_for(lambda: manager.status("svc").state is ServiceState.RUNNING, 10)
        pid = manager.status("svc").pid
        psutil.Process(pid).kill()
        assert wait_for(lambda: manager.status("svc").pid != pid, 10)
    assert wait_for(lambda: manager.status("svc").state is ServiceState.ERROR, 10)
    assert "automatic restarts" in manager.status("svc").message
    del status


def test_user_stop_is_not_a_crash(make_manager: MakeManager) -> None:
    port = free_port()
    manager = make_manager([fake_def("svc", port)])
    manager.start_monitor()
    manager.start("svc")
    manager.stop("svc")
    assert not wait_for(lambda: manager.status("svc").state is not ServiceState.STOPPED, 1.0)


def test_start_all_and_stop_all(make_manager: MakeManager) -> None:
    ports = [free_port(), free_port()]
    manager = make_manager([fake_def("a", ports[0]), fake_def("b", ports[1])])
    manager.start_all()
    assert all(s.state is ServiceState.RUNNING for s in manager.statuses())
    assert manager.any_running()
    manager.stop_all()
    assert all(s.state is ServiceState.STOPPED for s in manager.statuses())
    assert not manager.any_running()


def test_start_all_aggregates_errors(make_manager: MakeManager) -> None:
    good = fake_def("good", free_port())
    bad = fake_def("bad", free_port(), "--exit-immediately", "1")
    manager = make_manager([good, bad])
    with pytest.raises(ServiceError, match="1 service failed to start"):
        manager.start_all()
    assert manager.status("good").state is ServiceState.RUNNING


def test_missing_and_disabled_states(app_paths) -> None:  # type: ignore[no-untyped-def]
    from varsha.settings import Settings

    missing = ServiceDef(id="m", name="M", type="mysql", exe="bin/mysql/mysql-9*/bin/mysqld.exe")
    disabled = ServiceDef(id="d", name="D", type="mysql", exe="bin/x.exe", enabled=False)
    builtin = ServiceDef(id="dns", name="DNS", type="builtin")
    manager = ServiceManager(app_paths, [missing, disabled, builtin], Settings())
    states = {s.id: s.state for s in manager.statuses()}
    assert states == {"m": ServiceState.MISSING, "d": ServiceState.DISABLED, "dns": ServiceState.MISSING}
    assert "bin\\mysql\\mysql-9...\\bin\\mysqld.exe" in manager.status("m").message
    with pytest.raises(ServiceError, match="not installed"):
        manager.start("m")


def test_missing_config_file_is_explained(make_manager: MakeManager) -> None:
    defn = fake_def("svc", free_port())
    defn = ServiceDef(**{**defn.__dict__, "config_file": "etc/svc/svc.conf"})
    manager = make_manager([defn])
    with pytest.raises(ServiceError, match="config file etc/svc/svc.conf not found"):
        manager.start("svc")


def test_logs_roll_over_even_while_a_process_holds_them(tmp_path: Path) -> None:
    from varsha.services import process as proc_utils
    logs = tmp_path / "logs"
    logs.mkdir()
    big, small, own = logs / "apache.log", logs / "mailpit.log", logs / "varsha.log"
    big.write_bytes(b"x" * 3000)
    small.write_bytes(b"y" * 10)
    own.write_bytes(b"z" * 3000)  # varsha's own log rotates itself
    with open(big, "ab") as held:  # like a running service's inherited handle
        assert proc_utils.trim_logs(logs, 1000) == ["apache.log"]
        held.write(b"after")
    assert (logs / "apache.log.1").read_bytes() == b"x" * 3000
    assert big.read_bytes().strip(b"\0").endswith(b"after") and big.stat().st_size < 3000
    assert small.read_bytes() == b"y" * 10 and own.stat().st_size == 3000
    usage = proc_utils.logs_usage(logs)
    assert usage["count"] == 4 and usage["biggest"][0]["size"] == 3000


def test_restart_running_restarts_only_what_runs(make_manager) -> None:  # type: ignore[no-untyped-def]
    from conftest import free_port
    manager = make_manager([fake_def("one", free_port()), fake_def("two", free_port())])
    manager.start("one")
    first = manager.status("one").pid
    assert manager.restart_running() == [manager.definition("one").name]
    assert manager.status("one").state.value == "running" and manager.status("one").pid != first
    assert manager.status("two").state.value == "stopped"  # stopped services stay stopped
    manager.stop_all()
    assert manager.restart_running() == []


def test_only_processes_with_our_config_are_adopted(make_manager, app_paths) -> None:  # type: ignore[no-untyped-def]
    import dataclasses
    from conftest import free_port
    manager = make_manager([fake_def("svc", free_port())])
    root = str(app_paths.root).replace("\\", "/")
    mysql = dataclasses.replace(fake_def("db", 1), config_file="etc/mysql-5.6/my.ini", data_dir="data/mysql-5.6")
    ours = ["mysqld.exe", f"--defaults-file={root}/etc/mysql-5.6/my.ini", "--console"]
    assert manager._runs_our_config(mysql, ours)  # noqa: SLF001
    assert manager._runs_our_config(mysql, [a.upper().replace("/", "\\") for a in ours])  # case and slashes don't matter
    rescue = ["mysqld.exe", "--defaults-file=C:/Temp/job/work/my-level1.ini"]  # MySQL Rescue's temporary server
    test = ["mariadbd.exe", "--no-defaults", "--datadir=C:/Temp/bank-real/server-data", "--port=54108"]
    assert not manager._runs_our_config(mysql, rescue) and not manager._runs_our_config(mysql, test)  # noqa: SLF001
    mail = dataclasses.replace(fake_def("mail", 2), data_dir="data/mailpit")
    assert manager._runs_our_config(mail, ["mailpit.exe", "--database", f"{root}/data/mailpit/mailpit.db"])  # noqa: SLF001
    assert manager._runs_our_config(fake_def("plain", 3), ["anything"])  # neither: the exe alone decides  # noqa: SLF001


def test_built_in_services_of_another_varsha_are_not_adopted(make_manager, app_paths, tmp_path) -> None:  # type: ignore[no-untyped-def]
    import subprocess
    import sys
    import time
    from conftest import free_port
    manager = make_manager([fake_def("svc", free_port())])
    other = tmp_path / "other-varsha"
    other.mkdir()
    procs = [subprocess.Popen([sys._base_executable, "-c", "import time; time.sleep(30)"], cwd=str(cwd))  # type: ignore[attr-defined]
             for cwd in (app_paths.root, other)]
    try:
        time.sleep(0.5)
        ours, theirs = (psutil.Process(p.pid) for p in procs)
        assert manager._started_here(ours) is True  # noqa: SLF001
        assert manager._started_here(theirs) is False  # e.g. C:\Varsha's DNS seen from the checkout  # noqa: SLF001
    finally:
        for p in procs:
            p.kill()
            p.wait(10)
