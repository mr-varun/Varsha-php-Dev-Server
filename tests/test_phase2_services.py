from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import PYTHON, fake_def, free_port
from varsha.errors import ConfigError, ServiceError
from varsha.secrets import CredentialStore, client_option_file, option_value
from varsha.services.config import parse_services
from varsha.services.manager import ServiceManager, sql_string_body
from varsha.services.models import ServiceDef, ServiceState

FAKE_INIT = Path(__file__).with_name("fake_init.py")


def entry(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"id": "db", "name": "DB", "type": "mysql", "exe": "bin/db/mysqld.exe", "ports": [9300]}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------- services.json fields

def test_database_fields_parse() -> None:
    (defn,) = parse_services({"services": [entry(
        tools={"client": "{exe_dir}/mysql.exe", "dump": "{exe_dir}/mysqldump.exe"},
        password_reset_sql=["ALTER USER 'root'@'localhost' IDENTIFIED BY '{password}';", "FLUSH PRIVILEGES;"],
        init_command=["{exe}", "--bootstrap"], init_stdin=["sql:USE mysql;", "{home}/share/x.sql"],
        links=[{"label": "Inbox", "url": "http://127.0.0.1:{port}/"}],
        log_file="logs/db-error.log",
        stop={"method": "command", "command": ["{exe_dir}/mysqladmin.exe", "--defaults-extra-file={client_cnf}", "shutdown"],
              "also_kill": ["PHP-CGI.exe"]},
    )]})
    assert defn.is_database and defn.tools["dump"].endswith("mysqldump.exe")
    assert len(defn.password_reset_sql) == 2 and defn.links[0].label == "Inbox"
    assert defn.stop.also_kill == ("php-cgi.exe",)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"args": ["{client_cnf}"]}, "client_cnf"),          # only allowed in stop.command
        ({"tools": {"client": "{password}"}}, "password"),   # only allowed in password_reset_sql
        ({"init_stdin": ["sql:SELECT 1;"]}, "init_command"),
        ({"links": [{"label": "x"}]}, "url"),
        ({"tools": ["mysql.exe"]}, "tools"),
        ({"log_file": "C:/logs/x.log"}, "varsha folder"),
    ],
)
def test_database_field_errors(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_services({"services": [entry(**overrides)]})


def test_sql_string_body() -> None:
    assert sql_string_body("a'b\\c\nd") == "a\\'b\\\\c\\nd"  # newline escaped: --init-file is line based


# ---------------------------------------------------------------------- first-time setup

def init_def(port: int, *init_args: str, stdin: tuple[str, ...] = ()) -> ServiceDef:
    base = fake_def("db", port)
    return ServiceDef(**{**base.__dict__, "data_dir": "data/db",
                         "init_command": (str(PYTHON), str(FAKE_INIT), "{data_dir}", *init_args),
                         "init_stdin": stdin})


def test_init_runs_once_with_stdin(make_manager, app_paths) -> None:  # type: ignore[no-untyped-def]
    sql_file = app_paths.root / "seed.sql"
    sql_file.write_text("CREATE TABLE seeded;", encoding="utf-8")
    defn = init_def(free_port(), stdin=("sql:USE mysql;", str(sql_file)))
    manager: ServiceManager = make_manager([defn])
    manager.start("db")
    marker = app_paths.root / "data/db/initialised.txt"
    assert marker.read_text(encoding="utf-8").split() == ["USE", "mysql;", "CREATE", "TABLE", "seeded;"]
    manager.stop("db")
    marker.write_text("kept", encoding="utf-8")
    manager.start("db")  # data dir not empty any more: no second init
    assert marker.read_text(encoding="utf-8") == "kept"


def test_failed_init_is_moved_aside(make_manager, app_paths) -> None:  # type: ignore[no-untyped-def]
    manager: ServiceManager = make_manager([init_def(free_port(), "--fail")])
    with pytest.raises(ServiceError, match="first-time setup failed"):
        manager.start("db")
    data = app_paths.root / "data"
    assert not (data / "db").exists()
    assert any(p.name.startswith("db.failed-") for p in data.iterdir())
    assert manager.status("db").state is ServiceState.ERROR


def test_prepare_hook_runs_before_start(make_manager) -> None:  # type: ignore[no-untyped-def]
    seen: list[str] = []
    manager: ServiceManager = make_manager([fake_def("svc", free_port())])
    manager._prepare = lambda defn, inst: seen.append(defn.id)  # noqa: SLF001
    manager.start("svc")
    assert seen == ["svc"]

    def broken(defn: ServiceDef, inst: object) -> None:
        raise ConfigError("template exploded")

    manager.stop("svc")
    manager._prepare = broken  # noqa: SLF001
    with pytest.raises(ServiceError, match="template exploded"):
        manager.start("svc")


# ---------------------------------------------------------------------- credentials

@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_credentials_roundtrip(tmp_path: Path) -> None:
    store = CredentialStore(tmp_path / "credentials.json")
    assert store.get("db") == ""
    store.set("db", 'p"a\\ss ü')
    assert store.get("db") == 'p"a\\ss ü'
    assert 'p"a' not in (tmp_path / "credentials.json").read_text(encoding="utf-8")  # encrypted
    store.set("db", "")
    assert store.get("db") == ""


def test_corrupt_credentials_mean_empty(tmp_path: Path) -> None:
    (tmp_path / "credentials.json").write_text('{"db": "not-base64-dpapi!"}', encoding="utf-8")
    assert CredentialStore(tmp_path / "credentials.json").get("db") == ""


def test_client_option_file() -> None:
    assert option_value('a"b\\c') == '"a\\"b\\\\c"'
    with client_option_file(3307, "pw") as path:
        text = path.read_text(encoding="utf-8")
        assert "[client]" in text and "port=3307" in text and 'password="pw"' in text
    assert not path.exists()
