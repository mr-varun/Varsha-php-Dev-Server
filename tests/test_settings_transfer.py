"""Settings export / import: one zip of the config files, checked before anything is written."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from varsha import settings_transfer as st


def config(tmp_path: Path) -> Path:
    folder = tmp_path / "config"
    folder.mkdir(parents=True)
    (folder / "settings.json").write_text(json.dumps({"tld": "test", "php_timezone": "Asia/Kolkata"}), encoding="utf-8")
    (folder / "sites.json").write_text(json.dumps({"sites": {"banks": {"php": "5.4"}}}), encoding="utf-8")
    (folder / "notes.txt").write_text("not a varsha file", encoding="utf-8")
    return folder


def test_export_then_import_round_trip(tmp_path: Path) -> None:
    source = config(tmp_path / "a")
    body, name = st.export_zip(source, "0.1.0")
    assert name.startswith("varsha-settings-") and name.endswith(".zip")
    zip_path = tmp_path / name
    zip_path.write_bytes(body)
    with zipfile.ZipFile(zip_path) as zf:
        assert sorted(zf.namelist()) == ["settings.json", "sites.json", st.MANIFEST]  # only varsha's files
    target = config(tmp_path / "b")
    (target / "settings.json").write_text(json.dumps({"tld": "lan"}), encoding="utf-8")
    content = st.read_zip(zip_path)
    assert content["manifest"]["varsha"] == "0.1.0"
    rows = {r["name"]: r["status"] for r in st.preview(target, content["files"])}
    assert rows == {"settings.json": "changed", "sites.json": "same"}
    backup, written = st.apply(target, tmp_path / "b" / "backups", content["files"])
    assert sorted(written) == ["settings.json", "sites.json"]
    assert json.loads((target / "settings.json").read_text(encoding="utf-8"))["tld"] == "test"
    assert json.loads((backup / "settings.json").read_text(encoding="utf-8"))["tld"] == "lan"  # the old one is kept
    assert (target / "notes.txt").is_file()


def test_bad_zips_are_refused_before_anything_changes(tmp_path: Path) -> None:
    def make(entries: dict[str, str]) -> Path:
        path = tmp_path / f"z{len(list(tmp_path.glob('*.zip')))}.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, text in entries.items():
                zf.writestr(name, text)
        return path
    with pytest.raises(st.TransferError, match="no varsha settings"):
        st.read_zip(make({"../../evil.json": "{}", "sub/settings.json": "{}"}))  # paths are never followed
    with pytest.raises(st.TransferError, match="valid JSON"):
        st.read_zip(make({"settings.json": "{broken"}))
    with pytest.raises(st.TransferError, match="settings.json"):
        st.read_zip(make({"settings.json": json.dumps({"tld": "dev"})}))  # checked like varsha loads it
    with pytest.raises(st.TransferError, match="services.json"):
        st.read_zip(make({"services.json": json.dumps({"services": [{"id": "x"}]})}))
    (tmp_path / "plain.zip").write_text("not a zip", encoding="utf-8")
    with pytest.raises(st.TransferError, match="readable"):
        st.read_zip(tmp_path / "plain.zip")
