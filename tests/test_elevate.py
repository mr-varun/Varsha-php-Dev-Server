"""run_elevated: waiting for megha's result, also when Windows gives no process to wait on."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from varsha.errors import ServiceError
from varsha.helper import elevate
from varsha.paths import AppPaths


def fake_runas(monkeypatch: pytest.MonkeyPatch, outcome: dict[str, Any] | None, *, delay: float = 0.0,
               handle: bool = True, code: int | None = 0) -> list[list[str]]:
    """Replace the UAC launch: 'megha' writes `outcome` to --result after `delay` seconds."""
    calls: list[list[str]] = []

    def runas(exe: str, args: list[str], cwd: Path) -> tuple[int | None, bool]:
        calls.append([exe, *args])
        result = Path(args[args.index("--result") + 1])

        def write() -> None:
            time.sleep(delay)
            if outcome is not None:
                result.write_text(json.dumps(outcome), encoding="utf-8")

        if delay:
            threading.Thread(target=write, daemon=True).start()
        else:
            write()
        return code, handle

    monkeypatch.setattr(elevate, "_shell_execute_runas", runas)
    monkeypatch.setattr(elevate, "RESULT_WAIT", 3.0)
    monkeypatch.setattr(elevate, "RESULT_GRACE", 0.5)
    return calls


def test_normal_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_runas(monkeypatch, {"ok": True, "message": "hosts file updated: 2 names"})
    message = elevate.run_elevated(AppPaths(tmp_path), ["hosts-sync"], request={"names": ["a.test"]})
    assert message == "hosts file updated: 2 names"
    assert "--request" in calls[0] and "--result" in calls[0] and "hosts-sync" in calls[0]


def test_no_process_handle_waits_for_the_late_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Windows returned at once with no handle; megha writes its result a second later.
    fake_runas(monkeypatch, {"ok": True, "message": "done late"}, delay=1.0, handle=False, code=None)
    assert elevate.run_elevated(AppPaths(tmp_path), ["dns-route-remove"]) == "done late"


def test_missing_result_falls_back_to_checking_the_effect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_runas(monkeypatch, None, code=0)
    assert elevate.run_elevated(AppPaths(tmp_path), ["hosts-sync"], verify=lambda: "hosts file updated: 3 names") \
        == "hosts file updated: 3 names"


def test_missing_result_and_no_effect_is_an_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_runas(monkeypatch, None, code=0)
    with pytest.raises(ServiceError, match="without a result"):
        elevate.run_elevated(AppPaths(tmp_path), ["hosts-sync"], verify=lambda: None)


def test_megha_failure_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_runas(monkeypatch, {"ok": False, "message": "refusing host name 'x.com'"}, code=1)
    with pytest.raises(ServiceError, match="refusing host name"):
        elevate.run_elevated(AppPaths(tmp_path), ["hosts-sync"], verify=lambda: "should not be used")


def test_the_work_folder_is_always_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = fake_runas(monkeypatch, {"ok": True, "message": "ok"})
    elevate.run_elevated(AppPaths(tmp_path), ["dns-route-remove"])
    result = Path(calls[0][calls[0].index("--result") + 1])
    assert not result.parent.exists()


def test_a_result_file_that_is_still_empty_is_waited_for(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # megha's process ended, the file exists but is empty for longer than the grace period
    def runas(exe: str, args: list[str], cwd: Path) -> tuple[int | None, bool]:
        result = Path(args[args.index("--result") + 1])
        result.write_text("", encoding="utf-8")

        def fill() -> None:
            time.sleep(1.2)
            result.write_text(json.dumps({"ok": True, "message": "firewall rule added"}), encoding="utf-8")

        threading.Thread(target=fill, daemon=True).start()
        return 0, True

    monkeypatch.setattr(elevate, "_shell_execute_runas", runas)
    monkeypatch.setattr(elevate, "RESULT_WAIT", 3.0)
    monkeypatch.setattr(elevate, "RESULT_GRACE", 0.5)  # shorter than the 1.2 s the file stays empty
    assert elevate.run_elevated(AppPaths(tmp_path), ["firewall-lan-add"]) == "firewall rule added"


def test_megha_writes_its_result_atomically(tmp_path: Path) -> None:
    from varsha.helper import megha
    result = tmp_path / "result.json"
    megha.main(["dns-route-remove", "--result", str(result), "--tld", "no spaces allowed"])  # refused: no admin action runs
    assert json.loads(result.read_text(encoding="utf-8"))["ok"] is False
    assert not (tmp_path / "result.json.tmp").exists()
