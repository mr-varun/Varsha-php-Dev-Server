"""Database import wizard: reading data folders, copying safely, validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from varsha.errors import ServiceError
from varsha.services.importer import (
    _is_binlog,
    copy_data_folder,
    decode_folder_name,
    family_of_version,
    inspect_data_folder,
    quote_ident,
)


def make_folder(root: Path, kind: str) -> Path:
    data = root / kind
    for sysdb in ("mysql", "performance_schema", "sys"):
        (data / sysdb).mkdir(parents=True)
        (data / sysdb / "x.frm").write_bytes(b"s")
    (data / "ibdata1").write_bytes(b"i" * 100)
    if kind == "mariadb":
        (data / "aria_log_control").write_bytes(b"a")
        (data / "mysql_upgrade_info").write_text("11.8.9-MariaDB")
    elif kind == "mysql8":
        (data / "mysql.ibd").write_bytes(b"m" * 50)
        (data / "#innodb_redo").mkdir()
        (data / "#innodb_redo" / "#ib_redo1").write_bytes(b"r")
    else:
        (data / "mysql_upgrade_info").write_text("5.6.12\n")
    shop = data / "shop"
    shop.mkdir()
    for f in ("orders.frm", "orders.ibd", "items.ibd", "big#P#p0.ibd", "big#P#p1.ibd", "db.opt"):
        (shop / f).write_bytes(b"d" * 10)
    (data / "my@002dblog").mkdir()  # "my-blog" on disk
    (data / "my@002dblog" / "posts.ibd").write_bytes(b"p")
    (data / "binlog.000001").write_bytes(b"b" * 1000)
    (data / "mysql-bin.index").write_text("x")
    (data / "PC.err").write_text("old log")
    (data / "PC.pid").write_text("123")
    (data / "ib_logfile0").write_bytes(b"l")
    return data


@pytest.mark.parametrize("kind", ["mariadb", "mysql8", "mysql5"])
def test_inspect_detects_family_and_lists_databases(tmp_path: Path, kind: str) -> None:
    family, version, dbs = inspect_data_folder(make_folder(tmp_path, kind))
    assert family == kind
    assert version == {"mariadb": "11.8.9-MariaDB", "mysql8": "", "mysql5": "5.6.12"}[kind]
    by_name = {d["name"]: d for d in dbs}
    assert set(by_name) == {"shop", "my-blog"}  # system schemas and #innodb folders are not databases
    assert by_name["shop"]["tables"] == 3  # orders (frm+ibd), items, big (2 partitions)
    assert by_name["my-blog"]["folder"] == "my@002dblog"


def test_inspect_rejects_folders_that_are_not_data_folders(tmp_path: Path) -> None:
    (tmp_path / "random").mkdir()
    with pytest.raises(ServiceError, match="doesn't look like"):
        inspect_data_folder(tmp_path / "random")
    with pytest.raises(ServiceError, match="not a folder"):
        inspect_data_folder(tmp_path / "missing")


def test_copy_takes_system_files_and_only_the_chosen_databases(tmp_path: Path) -> None:
    source = make_folder(tmp_path, "mysql8")
    before = sorted((p.relative_to(source), p.stat().st_size) for p in source.rglob("*") if p.is_file())
    dest = tmp_path / "copy"
    copied = copy_data_folder(source, dest, {"shop"})
    names = {p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()}
    assert {"ibdata1", "mysql.ibd", "ib_logfile0", "#innodb_redo/#ib_redo1", "mysql/x.frm", "shop/orders.ibd"} <= names
    assert not any(n.startswith("my@002dblog") for n in names)  # not chosen
    assert not {"binlog.000001", "mysql-bin.index", "PC.err", "PC.pid"} & names  # logs are left out
    assert copied == sum((dest / n).stat().st_size for n in names)
    after = sorted((p.relative_to(source), p.stat().st_size) for p in source.rglob("*") if p.is_file())
    assert before == after  # the source is untouched


def test_helpers() -> None:
    assert decode_folder_name("my@002ddb@0024x") == "my-db$x"
    assert quote_ident("we`ird") == "`we``ird`"
    assert family_of_version("11.8.9-MariaDB") == "mariadb"
    assert family_of_version("8.4.3") == "mysql8" and family_of_version("5.6.12-log") == "mysql5"
    assert _is_binlog("binlog.000123") and _is_binlog("mysql-bin.index") and _is_binlog("pc-relay-bin.000002")
    assert not _is_binlog("ibdata1") and not _is_binlog("aria_log.00000001")


def test_the_copy_is_writable_even_when_the_source_is_read_only(tmp_path: Path) -> None:
    import os
    import stat

    source = make_folder(tmp_path, "mysql5")
    for p in source.rglob("*"):
        if p.is_file():
            os.chmod(p, stat.S_IREAD)  # e.g. a recovered folder copied from a CD or a backup
    try:
        dest = tmp_path / "copy"
        copy_data_folder(source, dest, {"shop"})
        for p in dest.rglob("*"):
            if p.is_file():
                p.write_bytes(b"server writes here")  # would raise PermissionError if read-only
        # ...while the source keeps its read-only flags (it is never modified)
        assert all(not (p.stat().st_mode & stat.S_IWRITE) for p in source.rglob("*") if p.is_file())
    finally:
        for p in source.rglob("*"):
            os.chmod(p, stat.S_IREAD | stat.S_IWRITE)


def test_folders_imported_from_are_offered_again(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from varsha.paths import AppPaths
    from varsha.services.importer import Importer

    manager = SimpleNamespace(paths=AppPaths(tmp_path / "root"), service_ids=[], definition=lambda sid: None)
    importer = Importer(manager, None, tmp_path / "root" / "data" / "import")  # type: ignore[arg-type]
    first, second = make_folder(tmp_path / "a", "mysql5"), make_folder(tmp_path / "b", "mariadb")
    importer._remember(str(first))  # noqa: SLF001 - what inspect_folder does
    importer._remember(str(second))  # noqa: SLF001
    importer._remember(str(first))  # noqa: SLF001 - used again: moves to the front, no duplicate
    picks = [p for p in importer.suggestions() if p.get("recent")]
    assert [p["path"] for p in picks] == [str(first), str(second)]
    assert picks[0]["family"] == "MySQL 5.x" and picks[0]["databases"] == 2
    import shutil
    shutil.rmtree(second)
    assert [p["path"] for p in importer.suggestions() if p.get("recent")] == [str(first)]  # gone folders drop out
