"""Export and import varsha's settings as one .zip (Settings page).

The zip holds the config files (settings, services, PHP, sites, scheduled jobs, remote databases,
the PHP error watcher) plus a manifest. Saved passwords are never in it: they're DPAPI-encrypted
for this Windows account (data\\credentials.json) and wouldn't work anywhere else anyway.

An import reads only the known file names at the zip's top level, checks every file the way
varsha itself loads it, and shows what would change. Applying it first copies the current files
to data\\settings-backups\\<time>\\, then writes the new ones; varsha restarts to use them.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from varsha.errors import ConfigError

FILES = ("settings.json", "services.json", "php.json", "sites.json", "schedule.json",
         "remote-databases.json", "php-errors.json")
MANIFEST = "varsha-settings.json"
MAX_FILE = 2 * 1024 * 1024
MAX_ZIP = 10 * 1024 * 1024


class TransferError(ValueError):
    pass


def export_zip(config_dir: Path, version: str) -> tuple[bytes, str]:
    """The zip's bytes and a file name like varsha-settings-20261106-1130.zip."""
    stamp = time.strftime("%Y%m%d-%H%M")
    buffer = io.BytesIO()
    included = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in FILES:
            path = config_dir / name
            if path.is_file():
                zf.write(path, name)
                included.append(name)
        zf.writestr(MANIFEST, json.dumps({"varsha": version, "exported": time.strftime("%Y-%m-%d %H:%M:%S"),
                                          "files": included,
                                          "note": "varsha settings. Saved passwords are not included."}, indent=2))
    return buffer.getvalue(), f"varsha-settings-{stamp}.zip"


def read_zip(path: Path) -> dict[str, Any]:
    """{"files": {name: bytes}, "manifest": {...}}; raises TransferError when it isn't usable."""
    try:
        if path.stat().st_size > MAX_ZIP:
            raise TransferError(f"{path.name} is bigger than {MAX_ZIP // 1048576} MB: not a varsha settings export")
        zf = zipfile.ZipFile(path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise TransferError(f"{path.name} isn't a readable .zip ({exc})") from None
    files: dict[str, bytes] = {}
    manifest: dict[str, Any] = {}
    with zf:
        for info in zf.infolist():
            name = info.filename
            if name not in (*FILES, MANIFEST):
                continue  # only known names at the top level: nothing else is ever written
            if info.file_size > MAX_FILE:
                raise TransferError(f"{name} in the zip is too big")
            data = zf.read(info)
            if name == MANIFEST:
                try:
                    manifest = json.loads(data.decode("utf-8"))
                except ValueError:
                    manifest = {}
                continue
            files[name] = data
    if not files:
        raise TransferError(f"{path.name} has no varsha settings files in it")
    for name, data in files.items():
        _validate(name, data)
    return {"files": files, "manifest": manifest if isinstance(manifest, dict) else {}}


def _validate(name: str, data: bytes) -> None:
    """Load the file the way varsha does, from a temporary copy."""
    try:
        raw = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TransferError(f"{name} in the zip isn't valid JSON ({exc})") from None
    if not isinstance(raw, dict):
        raise TransferError(f"{name} in the zip isn't a JSON object")
    with tempfile.TemporaryDirectory(prefix="varsha-import-") as tmp:
        path = Path(tmp) / name
        path.write_bytes(data)
        try:
            if name == "settings.json":
                from varsha.settings import load_settings
                load_settings(path)
            elif name == "services.json":
                from varsha.services.config import parse_services
                parse_services(raw, name)
            elif name == "sites.json":
                from varsha.sites.discovery import load_overrides
                load_overrides(path)
            elif name == "php.json":
                from varsha.php.config import PhpConfigStore
                PhpConfigStore(path).load()
        except ConfigError as exc:
            raise TransferError(f"{name} in the zip: {exc}") from None


def preview(config_dir: Path, files: dict[str, bytes]) -> list[dict[str, str]]:
    """What an import would do, per file: new / changed / same (files not in the zip stay)."""
    rows = []
    for name in FILES:
        current = config_dir / name
        if name not in files:
            rows.append({"name": name, "status": "kept" if current.is_file() else "none"})
            continue
        if not current.is_file():
            rows.append({"name": name, "status": "new"})
        else:
            same = _normal(current.read_bytes()) == _normal(files[name])
            rows.append({"name": name, "status": "same" if same else "changed"})
    return [r for r in rows if r["status"] != "none"]


def _normal(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n").strip()


def apply(config_dir: Path, backups_dir: Path, files: dict[str, bytes]) -> tuple[Path, list[str]]:
    """Back up the current files, then write the imported ones. Returns (backup folder, written)."""
    backup = backups_dir / time.strftime("%Y%m%d-%H%M%S")
    k = 2
    while backup.exists():
        backup = backups_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{k}"
        k += 1
    backup.mkdir(parents=True)
    for name in FILES:
        if (config_dir / name).is_file():
            shutil.copy2(config_dir / name, backup / name)
    written = []
    for name, data in files.items():
        if name not in FILES:
            continue
        target = config_dir / name
        tmp = target.with_suffix(".import-tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        written.append(name)
    return backup, written
