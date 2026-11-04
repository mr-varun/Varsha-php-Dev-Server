"""CSV / TSV on the Data page: parsed into a cache, edits written back in the file's own format."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from varsha.data import csv_files
from varsha.data.sqlite_files import DataError, DataFiles
from varsha.paths import AppPaths

CUSTOMERS = (
    "id,name,city,balance,code,note\r\n"
    "1,Asha,Hubli,1200.5,007,\"Hello, world\"\r\n"
    "2,Ravi,Dharwad,,010,\"two\r\nlines\"\r\n"
    "10,Meena,Hubli,15.25,100,\"she said \"\"hi\"\"\"\r\n"
)


@pytest.fixture
def files(tmp_path: Path) -> DataFiles:
    root = tmp_path / "varsha"
    AppPaths(root).ensure_dirs()
    return DataFiles(AppPaths(root))


def write(path: Path, text: str, encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode(encoding))
    return path


def test_parse_types_and_format(files: DataFiles, tmp_path: Path) -> None:
    path = write(tmp_path / "work" / "customers.csv", CUSTOMERS)
    fid = files.register(str(path))["id"]
    info = files.overview(fid)
    assert info["kind"] == "csv" and info["editable"] is True
    assert info["csv"] == {"encoding": "UTF-8", "delimiter": "comma", "header": True, "newline": "CRLF"}
    assert info["tables"] == [{"name": "customers", "type": "table", "rows": 3}]
    s = files.structure(fid, "customers")
    assert [(c["name"], c["type"]) for c in s["columns"]] == [
        ("id", "INTEGER"), ("name", "TEXT"), ("city", "TEXT"), ("balance", "REAL"), ("code", "TEXT"), ("note", "TEXT")]
    rows = files.rows(fid, "customers", order="id", desc=True)["rows"]
    assert [r["cells"][0]["v"] for r in rows] == [10, 2, 1]  # numbers sort as numbers
    assert rows[2]["cells"][4] == {"t": "text", "v": "007"}  # leading zeros stay
    assert rows[1]["cells"][5]["v"] == "two\r\nlines" and rows[0]["cells"][5]["v"] == 'she said "hi"'
    assert rows[1]["cells"][3] == {"t": "null"}
    assert files.rows(fid, "customers", search="hubli")["total"] == 2
    result = files.run_sql(fid, "SELECT city, sum(balance) AS total FROM customers GROUP BY city ORDER BY city")
    assert [(r[0]["v"], r[1].get("v")) for r in result["rows"]] == [("Dharwad", None), ("Hubli", 1215.75)]


@pytest.mark.parametrize("text, encoding, delimiter, label", [
    ("id;name\n1;Ganesh\n2;Gowri\n", "utf-8-sig", "semicolon", "UTF-8 with BOM"),
    ("id\tname\n1\tCafé\n", "cp1252", "tab", "Windows-1252"),
    ("id|name\n1|x\n", "utf-8", "pipe", "UTF-8"),
])
def test_delimiters_and_encodings(files: DataFiles, tmp_path: Path, text: str, encoding: str, delimiter: str, label: str) -> None:
    path = write(tmp_path / "f.csv", text, encoding)
    fid = files.register(str(path))["id"]
    info = files.overview(fid)
    assert info["csv"]["delimiter"] == delimiter and info["csv"]["encoding"] == label and info["csv"]["newline"] == "LF"
    assert files.rows(fid, "f")["rows"][0]["cells"][1]["v"] in ("Ganesh", "Café", "x")


def test_edits_are_written_back_in_the_same_format(files: DataFiles, tmp_path: Path) -> None:
    path = write(tmp_path / "customers.csv", CUSTOMERS)
    original = path.read_bytes()
    fid = files.register(str(path))["id"]
    with pytest.raises(DataError, match="read-only"):
        files.update_cell(fid, "customers", {"rowid": 1}, "city", {"t": "text", "v": "Gadag"})
    files.set_edit(fid, True)
    files.update_cell(fid, "customers", {"rowid": 1}, "city", {"t": "text", "v": "Gadag"})
    assert path.read_bytes() == original.replace(b"1,Asha,Hubli", b"1,Asha,Gadag")  # only that cell changed
    assert len(files.backups(fid)) == 1
    files.update_cell(fid, "customers", {"rowid": 1}, "city", {"t": "text", "v": "Hubli"})
    assert path.read_bytes() == original  # byte for byte: quoting, CRLF, "007", the two-line value
    files.insert_row(fid, "customers", {"id": {"t": "int", "v": 11}, "name": {"t": "text", "v": "Kiran; K"}})
    files.delete_rows(fid, "customers", [{"rowid": 2}])
    files.add_column(fid, "customers", "phone", "TEXT")
    lines = path.read_bytes().decode("utf-8").split("\r\n")
    assert lines[0] == "id,name,city,balance,code,note,phone"
    assert lines[-2] == "11,Kiran; K,,,,," and "Ravi" not in path.read_text(encoding="utf-8")
    assert len(files.backups(fid)) == 1  # one backup per session, before the first change
    # restore the original
    files.restore(fid, files.backups(fid)[-1]["name"])
    assert path.read_bytes() == original
    with pytest.raises(DataError, match="isn't available for CSV"):
        files.drop_table(fid, "customers")


def test_bom_and_encoding_survive_a_save(files: DataFiles, tmp_path: Path) -> None:
    path = write(tmp_path / "names.csv", "id;name\n1;Ganesh\n2;Gowri\n", "utf-8-sig")
    fid = files.register(str(path))["id"]
    files.set_edit(fid, True)
    files.update_cell(fid, "names", {"rowid": 2}, "name", {"t": "text", "v": "Gowri G"})
    assert path.read_bytes() == "﻿id;name\n1;Ganesh\n2;Gowri G\n".encode("utf-8")


def test_sql_box_is_read_only_for_csv(files: DataFiles, tmp_path: Path) -> None:
    path = write(tmp_path / "customers.csv", CUSTOMERS)
    fid = files.register(str(path))["id"]
    files.set_edit(fid, True)
    with pytest.raises(DataError, match="read-only"):
        files.run_sql(fid, "DELETE FROM customers")
    assert path.read_text(encoding="utf-8").count("Hubli") == 2


def test_cache_follows_the_file(files: DataFiles, tmp_path: Path) -> None:
    path = write(tmp_path / "customers.csv", CUSTOMERS)
    fid = files.register(str(path))["id"]
    assert files.overview(fid)["tables"][0]["rows"] == 3
    write(path, CUSTOMERS + "11,Suma,Haveri,1,2,x\r\n")
    os.utime(path, (time.time() + 5, time.time() + 5))
    assert files.overview(fid)["tables"][0]["rows"] == 4


def test_big_files_are_read_only_and_no_header_files(files: DataFiles, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write(tmp_path / "numbers.csv", "1,2,3\n4,5,6\n")
    fid = files.register(str(path))["id"]
    info = files.overview(fid)
    assert info["csv"]["header"] is False
    assert [c["name"] for c in files.structure(fid, "numbers")["columns"]] == ["column1", "column2", "column3"]
    monkeypatch.setattr("varsha.data.sqlite_files.EDIT_LIMIT", 5)
    with pytest.raises(DataError, match="read-only"):
        files.set_edit(fid, True)


def test_binary_files_are_refused(files: DataFiles, tmp_path: Path) -> None:
    path = tmp_path / "image.csv"
    path.write_bytes(b"\x89PNG\x00\x00\x00")
    with pytest.raises(DataError, match="isn't a SQLite, Access or CSV"):
        files.register(str(path))


def test_unique_names() -> None:
    assert csv_files.unique_names(["id", "Name", "name", "", "id"]) == ["id", "Name", "name_2", "column4", "id_2"]
