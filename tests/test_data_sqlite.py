"""The Data page's SQLite editor: read-only by default, backups before changes, safe SQL."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from varsha.data.sqlite_files import DataError, DataFiles, cell, from_cell, split_statements
from varsha.paths import AppPaths


@pytest.fixture
def setup(tmp_path: Path) -> tuple[DataFiles, Path]:
    root = tmp_path / "varsha"
    AppPaths(root).ensure_dirs()
    db = tmp_path / "projects" / "shop" / "database" / "shop.sqlite"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE customers (id INTEGER PRIMARY KEY, name TEXT NOT NULL, city TEXT, balance REAL, photo BLOB);
        INSERT INTO customers (name, city, balance, photo) VALUES
          ('Asha', 'Hubli', 1200.5, x'89504e47'), ('Ravi', 'Dharwad', NULL, NULL), ('Meena', 'Hubli', 15, NULL);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT) WITHOUT ROWID;
        INSERT INTO settings VALUES ('theme', 'dark'), ('lang', 'kn');
        CREATE VIEW hubli AS SELECT * FROM customers WHERE city = 'Hubli';
        CREATE INDEX customers_city ON customers(city);
    """)
    conn.commit()
    conn.close()
    files = DataFiles(AppPaths(root))
    return files, db


def opened(setup: tuple[DataFiles, Path]) -> tuple[DataFiles, str, Path]:
    files, db = setup
    return files, files.register(str(db))["id"], db


def test_register_checks_the_file(setup: tuple[DataFiles, Path], tmp_path: Path) -> None:
    files, db = setup
    info = files.register(str(db))
    assert info["name"] == "shop.sqlite" and len(info["id"]) == 16
    assert [r["id"] for r in files.listing()["recent"]] == [info["id"]]
    paradox = tmp_path / "old.db"
    paradox.write_bytes(b"\x05\x00not sqlite")
    with pytest.raises(DataError, match="isn't a SQLite database"):
        files.register(str(paradox))
    inside = files.paths.data / "mysql.sqlite"
    inside.write_bytes(db.read_bytes())
    with pytest.raises(DataError, match="data folder"):
        files.register(str(inside))
    with pytest.raises(DataError, match="doesn't exist"):
        files.register(str(tmp_path / "nope.sqlite"))
    with pytest.raises(DataError, match="Unknown file"):
        files.path("0" * 16)


def test_overview_and_structure(setup: tuple[DataFiles, Path]) -> None:
    files, fid, _db = opened(setup)
    info = files.overview(fid)
    assert {(t["name"], t["type"], t["rows"]) for t in info["tables"]} == {
        ("customers", "table", 3), ("settings", "table", 2), ("hubli", "view", 2)}
    assert info["indexes"] == 1 and info["edit"] is False
    assert [t["name"] for t in info["tables"]] == ["customers", "settings", "hubli"]  # tables first, then views
    s = files.structure(fid, "CUSTOMERS")  # names match case-insensitively
    assert s["name"] == "customers" and s["rowid"] is True
    assert [c["name"] for c in s["columns"]] == ["id", "name", "city", "balance", "photo"]
    assert s["indexes"][0]["columns"] == ["city"]
    assert files.structure(fid, "settings")["rowid"] is False


def test_rows_paging_sorting_and_search(setup: tuple[DataFiles, Path]) -> None:
    files, fid, _db = opened(setup)
    page = files.rows(fid, "customers", limit=2)
    assert page["total"] == 3 and len(page["rows"]) == 2 and page["editable"]
    assert page["rows"][0]["key"] == {"rowid": 1}
    assert page["rows"][0]["cells"][4] == {"t": "blob", "size": 4, "hex": "89504e47"}
    assert page["rows"][1]["cells"][3] == {"t": "null"}
    by_name = files.rows(fid, "customers", order="name", desc=True)
    assert [r["cells"][1]["v"] for r in by_name["rows"]] == ["Ravi", "Meena", "Asha"]
    found = files.rows(fid, "customers", search="hub")
    assert found["total"] == 2
    assert files.rows(fid, "customers", search="1200", column="balance")["total"] == 1
    assert files.rows(fid, "settings")["rows"][0]["key"]["pk"]  # WITHOUT ROWID: primary key
    assert files.rows(fid, "hubli")["editable"] is False  # views
    with pytest.raises(DataError, match="No column"):
        files.rows(fid, "customers", order="nope; DROP TABLE customers")
    with pytest.raises(DataError, match="No table"):
        files.rows(fid, 'customers"; DROP TABLE customers; --')


def test_read_only_until_edit_mode_and_backup_first(setup: tuple[DataFiles, Path]) -> None:
    files, fid, db = opened(setup)
    with pytest.raises(DataError, match="read-only"):
        files.update_cell(fid, "customers", {"rowid": 2}, "city", {"t": "text", "v": "Gadag"})
    with pytest.raises(DataError, match="read-only"):
        files.run_sql(fid, "UPDATE customers SET city = 'X'")
    assert files.backups(fid) == []
    files.set_edit(fid, True)
    assert files.update_cell(fid, "customers", {"rowid": 2}, "city", {"t": "text", "v": "Gadag"}) == "city saved"
    assert len(files.backups(fid)) == 1  # taken before the first change only
    files.update_cell(fid, "customers", {"rowid": 2}, "balance", {"t": "real", "v": "99.5"})
    files.update_cell(fid, "customers", {"rowid": 3}, "city", {"t": "null"})
    assert len(files.backups(fid)) == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT city, balance FROM customers WHERE id = 2").fetchone() == ("Gadag", 99.5)
        assert conn.execute("SELECT city FROM customers WHERE id = 3").fetchone() == (None,)
    with pytest.raises(DataError, match="valid integer"):
        files.update_cell(fid, "customers", {"rowid": 2}, "id", {"t": "int", "v": "abc"})
    with pytest.raises(DataError, match="gone"):
        files.update_cell(fid, "customers", {"rowid": 99}, "city", {"t": "text", "v": "x"})
    with pytest.raises(DataError, match="Views"):
        files.update_cell(fid, "hubli", {"rowid": 1}, "city", {"t": "text", "v": "x"})


def test_insert_delete_and_primary_key_tables(setup: tuple[DataFiles, Path]) -> None:
    files, fid, db = opened(setup)
    files.set_edit(fid, True)
    assert "rowid 4" in files.insert_row(fid, "customers", {"name": {"t": "text", "v": "Kiran"}, "balance": {"t": "int", "v": 5}})
    with pytest.raises(DataError):  # NOT NULL name
        files.insert_row(fid, "customers", {"city": {"t": "text", "v": "x"}})
    assert files.delete_rows(fid, "customers", [{"rowid": 1}, {"rowid": 4}]) == "2 rows deleted from customers"
    key = files.rows(fid, "settings", order="key")["rows"][0]["key"]  # 'lang'
    files.update_cell(fid, "settings", key, "value", {"t": "text", "v": "en"})
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM customers").fetchone() == (2,)
        assert conn.execute("SELECT value FROM settings WHERE key = 'lang'").fetchone() == ("en",)
    with pytest.raises(DataError, match="primary key"):
        files.update_cell(fid, "settings", {"pk": {"other": {"t": "text", "v": "x"}}}, "value", {"t": "null"})


def test_sql_box(setup: tuple[DataFiles, Path], tmp_path: Path) -> None:
    files, fid, db = opened(setup)
    result = files.run_sql(fid, "SELECT name, balance FROM customers WHERE city = 'Hubli' ORDER BY name")
    assert result["columns"] == ["name", "balance"] and [r[0]["v"] for r in result["rows"]] == ["Asha", "Meena"]
    files.set_edit(fid, True)
    result = files.run_sql(fid, "UPDATE customers SET city = 'Hubballi' WHERE city = 'Hubli'; SELECT count(*) FROM customers WHERE city = 'Hubballi';")
    assert result["changed"] == 2 and result["rows"][0][0]["v"] == 2 and result["statements"] == 2
    # a failing statement rolls back the whole script
    with pytest.raises(DataError):
        files.run_sql(fid, "DELETE FROM customers; INSERT INTO nope VALUES (1);")
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM customers").fetchone() == (3,)
    other = tmp_path / "other.sqlite"
    for sql in (f"ATTACH DATABASE '{other}' AS x", f"VACUUM INTO '{other}'", "VACUUM"):
        with pytest.raises(DataError, match="not allowed"):
            files.run_sql(fid, sql)
    assert not other.exists()


def test_restore_puts_a_backup_back(setup: tuple[DataFiles, Path]) -> None:
    files, fid, db = opened(setup)
    files.set_edit(fid, True)
    files.run_sql(fid, "DELETE FROM customers")
    first = files.backups(fid)[-1]["name"]
    assert "restored" in files.restore(fid, first)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM customers").fetchone() == (3,)
    assert len(files.backups(fid)) == 2  # the emptied state was kept too
    with pytest.raises(DataError, match="No such backup"):
        files.restore(fid, "..\\..\\shop.sqlite")


def test_structure_changes_and_exports(setup: tuple[DataFiles, Path]) -> None:
    files, fid, _db = opened(setup)
    files.set_edit(fid, True)
    files.add_column(fid, "customers", "phone", "text", {"t": "text", "v": "n/a"})
    assert files.rows(fid, "customers")["rows"][0]["cells"][-1] == {"t": "text", "v": "n/a"}
    with pytest.raises(DataError, match="Column names"):
        files.add_column(fid, "customers", "x; DROP TABLE customers", "TEXT")
    body, name = files.export_csv(fid, "customers")
    text = body.decode("utf-8-sig")
    assert name == "customers.csv" and text.splitlines()[0] == "id,name,city,balance,photo,phone"
    assert "Asha,Hubli,1200.5,89504e47,n/a" in text
    sql, sql_name = files.export_sql(fid)
    assert sql_name == "shop.sql" and b"CREATE TABLE customers" in sql
    assert files.integrity(fid) == "Integrity check: ok"
    assert "Vacuum done" in files.vacuum(fid)
    assert "dropped" in files.drop_table(fid, "hubli")


def test_found_in_sites_follows_junctions(setup: tuple[DataFiles, Path]) -> None:
    import _winapi

    files, db = setup
    project = db.parents[1]
    (project / "public").mkdir()
    _winapi.CreateJunction(str(project / "public"), str(files.paths.www / "shop"))
    found = files.listing()["found"]
    assert [(f["site"], f["name"]) for f in found] == [("shop", "shop.sqlite")]
    assert files.overview(found[0]["id"])["tables"]  # usable by id without registering


def test_helpers() -> None:
    assert split_statements("SELECT ';'; -- a; comment\nSELECT 2") == ["SELECT ';';", "-- a; comment\nSELECT 2"]
    assert split_statements(" ; ") == []
    assert cell(2**60) == {"t": "int", "v": str(2**60), "big": True}
    assert cell("x" * 3000)["more"] == 3000
    assert from_cell({"t": "int", "v": " 42 "}) == 42 and from_cell({"t": "null"}) is None
    with pytest.raises(DataError):
        from_cell({"t": "blob", "v": "00"})
