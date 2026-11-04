from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import REPO_ROOT
from varsha.errors import ConfigError
from varsha.services.config import load_services, parse_services
from varsha.services.models import StopMethod


def entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "svc",
        "name": "Service",
        "type": "generic",
        "exe": "bin/svc/svc.exe",
        "ports": [9000],
    }
    base.update(overrides)
    return base


def parse(*entries: dict[str, Any]) -> list[Any]:
    return parse_services({"services": list(entries)})


def test_shipped_services_json_is_valid() -> None:
    services = load_services(REPO_ROOT / "config" / "services.json")
    ids = [s.id for s in services]
    assert {"apache", "mariadb-11.8", "mysql-8.4", "mysql-5.6", "mailpit", "dns"} <= set(ids)
    by_id = {s.id: s for s in services}
    assert by_id["apache"].stop.method is StopMethod.APACHE_SIGNAL
    assert by_id["mysql-8.4"].ports == (3307,)
    assert by_id["dns"].is_builtin and by_id["dns"].exe is None


def test_new_service_needs_no_code_change() -> None:
    (redis,) = parse(entry(id="redis", name="Redis", type="redis", exe="bin/redis/*/redis-server.exe",
                           args=["--port", "{port}"], ports=[6379]))
    assert redis.stop.method is StopMethod.TERMINATE
    assert redis.args == ("--port", "{port}")


@pytest.mark.parametrize("exe", ["C:/laragon/bin/httpd.exe", "C:\\x\\a.exe", "\\bin\\a.exe",
                                 "bin/../../a.exe"])
def test_rejects_non_portable_paths(exe: str) -> None:
    with pytest.raises(ConfigError, match="varsha folder"):
        parse(entry(exe=exe))


def test_rejects_duplicate_ids() -> None:
    with pytest.raises(ConfigError, match="duplicate service id"):
        parse(entry(), entry(ports=[9001]))


def test_rejects_shared_port_between_enabled_services() -> None:
    with pytest.raises(ConfigError, match="port 9000 is used by both"):
        parse(entry(id="a"), entry(id="b"))


def test_disabled_service_may_share_a_port() -> None:
    services = parse(entry(id="a"), entry(id="b", enabled=False))
    assert [s.enabled for s in services] == [True, False]


@pytest.mark.parametrize("ports", [[0], [70000], ["80"], [True], [80, 80]])
def test_rejects_bad_ports(ports: list[Any]) -> None:
    with pytest.raises(ConfigError, match="port"):
        parse(entry(ports=ports))


def test_unknown_placeholder_is_rejected() -> None:
    with pytest.raises(ConfigError, match=r"\{nope\}"):
        parse(entry(args=["{nope}"]))


def test_config_placeholder_requires_config_file() -> None:
    with pytest.raises(ConfigError, match="config_file"):
        parse(entry(args=["--defaults-file={config}"]))


def test_command_stop_needs_command() -> None:
    with pytest.raises(ConfigError, match="stop.command"):
        parse(entry(stop={"method": "command"}))


def test_bad_stop_method() -> None:
    with pytest.raises(ConfigError, match="not one of"):
        parse(entry(stop={"method": "explode"}))


def test_underscore_keys_are_comments(caplog: pytest.LogCaptureFixture) -> None:
    parse(entry(_note="hello"))
    assert "unknown key" not in caplog.text
    parse(entry(colour="blue"))
    assert "unknown key 'colour'" in caplog.text


def test_non_builtin_needs_exe() -> None:
    with pytest.raises(ConfigError, match='"exe" is required'):
        parse(entry(exe=None))


def test_invalid_json_reports_position(tmp_path: Path) -> None:
    path = tmp_path / "services.json"
    path.write_text('{"services": [,]}', encoding="utf-8")
    with pytest.raises(ConfigError, match="line 1"):
        load_services(path)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_services(tmp_path / "nope.json")


def test_roundtrip_from_file(tmp_path: Path) -> None:
    path = tmp_path / "services.json"
    path.write_text(json.dumps({"services": [entry()]}), encoding="utf-8")
    assert load_services(path)[0].id == "svc"
