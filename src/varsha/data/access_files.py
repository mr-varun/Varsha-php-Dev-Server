"""Access databases (.mdb / .accdb) on the Data page: read-only, through a 32-bit helper.

The Access ODBC driver on this PC is 32-bit, so varsha (64-bit) can't load it. Each request runs
access_odbc.py under a 32-bit Python that is already installed (bin\\python32\\, then the usual
per-user / Program Files (x86) locations, or VARSHA_PYTHON32) with a JSON request on stdin.

The user's file is never opened, not even read-only: Access writes a .ldb lock file next to every
database it opens, and these are often live files (e.g. a banking program's .mdb). varsha copies
the file into data\\access-cache\\ and reads the copy; the copy is refreshed whenever the
original's size or modification time changes.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from varsha.paths import AppPaths

logger = logging.getLogger(__name__)

ACCESS_EXTENSIONS = (".mdb", ".accdb", ".mde", ".accde")
HELPER = Path(__file__).with_name("access_odbc.py")
CALL_TIMEOUT = 120
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class AccessError(ValueError):
    pass


def is_access(path: Path) -> bool:
    """Jet (.mdb) and ACE (.accdb) files say so in their first page."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(32)
    except OSError:
        return False
    return head[4:19] in (b"Standard Jet DB", b"Standard ACE DB")


def engine_name(path: Path) -> str:
    try:
        with open(path, "rb") as handle:
            head = handle.read(32)
    except OSError:
        return "Access"
    return "Access 2007+ (ACE)" if head[4:19] == b"Standard ACE DB" else "Access 97-2003 (Jet)"


def find_python32(paths: AppPaths) -> Path | None:
    override = os.environ.get("VARSHA_PYTHON32")
    candidates: list[Path] = [Path(override)] if override else []
    candidates += sorted(paths.bin.glob("python32*/python.exe"), reverse=True)
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Python"
    candidates += sorted(local.glob("Python3*-32/python.exe"), key=lambda p: p.parent.name, reverse=True)
    candidates += sorted(Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")).glob("Python3*/python.exe"),
                         reverse=True)
    return next((p for p in candidates if p.is_file()), None)


class AccessReader:
    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self.cache = paths.data / "access-cache"
        self._locks: dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    @property
    def python32(self) -> Path | None:
        return find_python32(self.paths)

    def available(self) -> str:
        """"" when Access files can be read, else why not."""
        if self.python32 is None:
            return ("Reading Access files needs a 32-bit Python (the Access driver here is 32-bit). Install Python 3 "
                    "(32-bit), or copy one to bin\\python32\\.")
        if not HELPER.is_file():
            return "access_odbc.py is missing from this varsha build"
        return ""

    def snapshot(self, file_id: str, source: Path) -> tuple[Path, float]:
        """The cached copy of `source`, refreshed when the original changed. Returns (copy, taken_at)."""
        with self._lock:
            lock = self._locks.setdefault(file_id, threading.Lock())
        with lock:
            self.cache.mkdir(parents=True, exist_ok=True)
            copy = self.cache / f"{file_id}{source.suffix.lower()}"
            meta = self.cache / f"{file_id}.json"
            stat = source.stat()
            stamp = {"size": stat.st_size, "mtime": stat.st_mtime_ns, "source": str(source)}
            try:
                old = json.loads(meta.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                old = {}
            if not copy.is_file() or {k: old.get(k) for k in stamp} != stamp:
                tmp = copy.with_suffix(copy.suffix + ".tmp")
                shutil.copyfile(source, tmp)  # plain read of the original; nothing is written next to it
                os.replace(tmp, copy)
                copy.with_suffix(".ldb").unlink(missing_ok=True)
                copy.with_suffix(".laccdb").unlink(missing_ok=True)
                stamp["taken"] = time.time()
                meta.write_text(json.dumps(stamp), encoding="utf-8")
                logger.info("Access snapshot of %s (%s bytes)", source, stat.st_size)
                return copy, stamp["taken"]
            return copy, float(old.get("taken") or 0)

    def forget(self, file_id: str) -> None:
        for name in (f"{file_id}.json", *(f"{file_id}{ext}" for ext in ACCESS_EXTENSIONS)):
            (self.cache / name).unlink(missing_ok=True)

    def call(self, file_id: str, source: Path, op: str, **request: Any) -> Any:
        problem = self.available()
        if problem:
            raise AccessError(problem)
        copy, _taken = self.snapshot(file_id, source)
        payload = json.dumps({"file": str(copy), "op": op, **request}).encode("utf-8")
        try:
            result = subprocess.run([str(self.python32), "-I", "-X", "utf8", str(HELPER)], input=payload,
                                    capture_output=True, timeout=CALL_TIMEOUT, cwd=str(self.cache),
                                    creationflags=CREATE_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise AccessError(f"Reading {source.name} took longer than {CALL_TIMEOUT} s") from None
        except OSError as exc:
            raise AccessError(f"The 32-bit Python couldn't start: {exc}") from exc
        try:
            answer = json.loads(result.stdout.decode("utf-8"))
        except ValueError:
            detail = (result.stderr or result.stdout).decode("utf-8", "replace").strip()[-400:]
            raise AccessError(f"The Access helper failed: {detail or f'exit {result.returncode}'}") from None
        if not answer.get("ok"):
            message = str(answer.get("error") or "unknown error")
            if "password" in message.lower():
                message += " (password-protected Access files aren't supported yet)"
            raise AccessError(message)
        return answer["result"]
