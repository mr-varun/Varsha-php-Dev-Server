"""Settings page: start with Windows, auto-start services, the control panel port."""

from __future__ import annotations

import json
import socket
import uuid
from pathlib import Path

import pytest

from conftest import fake_def
from varsha import app_settings
from varsha.app_settings import StartupEntry, check_panel_port, set_autostart_flags
from varsha.errors import ConfigError


@pytest.fixture
def entry():  # a private key, never the real Run key
    key = r"Software\varsha-tests-%s\Run" % uuid.uuid4().hex[:8]
    yield StartupEntry(key=key, name="varsha-test")
    import winreg
    for sub in (key, key.rsplit("\\", 1)[0]):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except OSError:
            pass


def test_start_with_windows_writes_and_removes_the_run_entry(entry: StartupEntry) -> None:
    assert entry.status()["enabled"] is False
    assert "starts with Windows" in entry.set(True)
    status = entry.status()
    assert status["enabled"] and status["matches"]
    assert entry.current() == app_settings.startup_command()
    assert app_settings.STARTUP_FLAG in entry.current()  # starts quietly in the tray
    assert "no longer" in entry.set(False)
    assert entry.status()["enabled"] is False
    entry.set(False)  # switching off twice is fine


def test_dev_and_deployed_copies_use_their_own_entry() -> None:
    assert app_settings.startup_name() == "varsha (dev)"  # tests run from source; Varsha.exe uses "varsha"
    assert "-m varsha" in app_settings.startup_command()


SERVICES = {
    "_comment": "kept as it is",
    "services": [
        {"id": "apache", "name": "Apache", "autostart": False, "_note": "kept"},
        {"id": "mariadb-11.8", "name": "MariaDB", "autostart": True},
        {"id": "mailpit", "name": "Mailpit"},
    ],
}


def test_autostart_flags_are_written_into_services_json(tmp_path: Path) -> None:
    path = tmp_path / "services.json"
    path.write_text(json.dumps(SERVICES), encoding="utf-8")
    assert sorted(set_autostart_flags(path, {"apache", "mailpit"})) == ["apache", "mailpit", "mariadb-11.8"]
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert [s.get("autostart") for s in raw["services"]] == [True, False, True]
    assert raw["_comment"] == "kept as it is" and raw["services"][0]["_note"] == "kept"
    assert set_autostart_flags(path, {"apache", "mailpit"}) == []  # nothing to change
    with pytest.raises(ConfigError, match="Unknown services"):
        set_autostart_flags(path, {"nope"})


def test_the_manager_takes_the_new_flag_at_once(make_manager) -> None:  # type: ignore[no-untyped-def]
    manager = make_manager([fake_def("svc", 0)])
    assert manager.definition("svc").autostart is False
    manager.set_autostart("svc", True)
    assert manager.definition("svc").autostart is True


def test_panel_port_checks() -> None:
    assert check_panel_port("7700", 7700, set()) == 7700  # the current port is always fine
    with pytest.raises(ValueError, match="number"):
        check_panel_port("abc", 7700, set())
    for bad in (80, 70000):
        with pytest.raises(ValueError, match="1024 to 65535"):
            check_panel_port(bad, 7700, set())
    with pytest.raises(ValueError, match="services"):
        check_panel_port(3306, 7700, {3306, 80})
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        with pytest.raises(ValueError, match="in use"):
            check_panel_port(port, 7700, set())
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert check_panel_port(free, 7700, set()) == free
