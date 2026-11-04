"""Access files on the Data page: read through the 32-bit helper, from a copy, never written."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from varsha.data.access_files import AccessReader, find_python32, is_access
from varsha.data.sqlite_files import DataError, DataFiles
from varsha.paths import AppPaths

HERE = Path(__file__).parent
PY32 = find_python32(AppPaths(HERE.parent))
needs_access = pytest.mark.skipif(PY32 is None, reason="needs a 32-bit Python and the Access ODBC driver")


@pytest.fixture(scope="module")
def mdb(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("bank") / "bank.mdb"
    subprocess.run([str(PY32), "-I", str(HERE / "make_access_fixture.py"), str(path)], check=True, capture_output=True, timeout=120)
    # the fixture's own (writable) connection left a lock file behind; the tests check none appears later
    path.with_suffix(".ldb").unlink(missing_ok=True)
    return path


@pytest.fixture
def files(tmp_path: Path) -> DataFiles:
    root = tmp_path / "varsha"
    AppPaths(root).ensure_dirs()
    return DataFiles(AppPaths(root))


@needs_access
def test_access_file_is_read_from_a_copy(files: DataFiles, mdb: Path) -> None:
    before = (mdb.stat().st_mtime_ns, mdb.read_bytes())
    fid = files.register(str(mdb))["id"]
    info = files.overview(fid)
    assert info["kind"] == "access" and info["engine"].startswith("Access 97") and info["edit"] is False
    assert [(t["name"], t["rows"]) for t in info["tables"]] == [("loans", 10), ("members", 30)]
    s = files.structure(fid, "MEMBERS")
    assert [c["name"] for c in s["columns"]][:3] == ["id", "name", "branch"] and s["columns"][0]["pk"] == 1
    assert s["indexes"][0]["name"] == "PrimaryKey"
    # never touched: same bytes, same time, no lock file next to it
    assert (mdb.stat().st_mtime_ns, mdb.read_bytes()) == before
    assert sorted(p.name for p in mdb.parent.iterdir()) == ["bank.mdb"]


@needs_access
def test_access_rows_paging_search_sql_and_csv(files: DataFiles, mdb: Path) -> None:
    fid = files.register(str(mdb))["id"]
    page = files.rows(fid, "members", offset=28, limit=5, order="id")
    assert page["total"] == 30 and [r["cells"][0]["v"] for r in page["rows"]] == [29, 30]
    assert page["editable"] is False and page["rows"][0]["key"] is None
    assert files.rows(fid, "members", search="Dharwad")["total"] == 15
    assert files.rows(fid, "members", search="25", column="name")["total"] == 1
    long = files.rows(fid, "members", search="long note")["rows"][0]["cells"][6]
    assert long["more"] == 3000
    result = files.run_sql(fid, "SELECT branch, COUNT(*) AS n FROM members GROUP BY branch ORDER BY branch")
    assert result["columns"] == ["branch", "n"] and [r[1]["v"] for r in result["rows"]] == [15, 15]
    body, name = files.export_csv(fid, "loans")
    assert name == "loans.csv" and body.decode("utf-8-sig").splitlines()[1] == "L0001,1,1000.5"
    with pytest.raises(DataError, match="No column"):
        files.rows(fid, "members", order="id; DROP TABLE members")


@needs_access
def test_access_is_never_written(files: DataFiles, mdb: Path) -> None:
    fid = files.register(str(mdb))["id"]
    for sql in ("DELETE FROM loans", "UPDATE loans SET amount = 0", "DROP TABLE loans"):
        with pytest.raises(DataError, match="SELECT"):
            files.run_sql(fid, sql)
    with pytest.raises(DataError, match="read-only"):
        files.set_edit(fid, True)
    for action in (lambda: files.backup(fid), lambda: files.vacuum(fid), lambda: files.export_sql(fid),
                   lambda: files.update_cell(fid, "loans", {"rowid": 1}, "amount", {"t": "int", "v": 1})):
        with pytest.raises(DataError, match="read-only"):
            action()


@needs_access
def test_snapshot_follows_changes(files: DataFiles, mdb: Path, tmp_path: Path) -> None:
    copy_of = tmp_path / "copy.mdb"
    copy_of.write_bytes(mdb.read_bytes())
    fid = files.register(str(copy_of))["id"]
    reader: AccessReader = files.access
    first, taken = reader.snapshot(fid, copy_of)
    assert reader.snapshot(fid, copy_of) == (first, taken)  # unchanged: the same copy
    copy_of.write_bytes(copy_of.read_bytes() + b"\0" * 4096)  # the original changed
    _again, taken2 = reader.snapshot(fid, copy_of)
    assert taken2 >= taken and first.stat().st_size == copy_of.stat().st_size


@needs_access
def test_accdb_files_work_too(files: DataFiles, tmp_path: Path) -> None:
    accdb = tmp_path / "new.accdb"
    made = subprocess.run([str(PY32), "-I", str(HERE / "make_access_fixture.py"), str(accdb)], capture_output=True, timeout=120)
    if made.returncode != 0:
        pytest.skip("the ACE (.accdb) driver isn't installed")
    accdb.with_suffix(".laccdb").unlink(missing_ok=True)
    fid = files.register(str(accdb))["id"]
    info = files.overview(fid)
    assert info["engine"].startswith("Access 2007") and [t["rows"] for t in info["tables"]] == [10, 30]
    assert files.rows(fid, "loans", limit=2)["rows"][1]["cells"][0]["v"] == "L0002"


def test_detection(tmp_path: Path) -> None:
    jet = tmp_path / "a.mdb"
    jet.write_bytes(b"\x00\x01\x00\x00Standard Jet DB\x00" + b"\0" * 100)
    assert is_access(jet)
    other = tmp_path / "b.mdb"
    other.write_bytes(b"not an access file")
    assert not is_access(other)


def test_missing_32bit_python_is_explained(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("varsha.data.access_files.find_python32", lambda paths: None)
    reader = AccessReader(AppPaths(tmp_path))
    assert "32-bit Python" in reader.available()
