"""Remember database root passwords, encrypted with Windows DPAPI.

Stored in data/credentials.json. DPAPI ties the encryption to the current Windows user, so
another account on this PC can't read them. After copying varsha to another PC or account,
decryption fails and the password is simply treated as unknown (set it again).

Passwords are handed to MySQL tools through a temporary [client] option file in the user's
private %TEMP% folder, never on the command line, where other processes could see them.
"""

from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import logging
import os
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

_ENTROPY = b"varsha-db-credentials"


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":  # pragma: no cover - varsha is Windows-only
        raise OSError("DPAPI is only available on Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    source, _keep1 = _blob(data)
    entropy, _keep2 = _blob(_ENTROPY)
    out = _Blob()
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = func(ctypes.byref(source), None, ctypes.byref(entropy), None, None, 0x1,  # UI_FORBIDDEN
              ctypes.byref(out))
    if not ok:
        raise OSError(f"DPAPI failed (error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


class CredentialStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict[str, str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable %s: %s", self.path.name, exc)
            return {}
        return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}

    def get(self, service_id: str) -> str:
        """The saved root password, or "" (a fresh install has no root password)."""
        with self._lock:
            token = self._load().get(service_id)
        if not token:
            return ""
        try:
            return _dpapi(base64.b64decode(token), protect=False).decode("utf-8")
        except (OSError, ValueError) as exc:
            logger.warning("Saved password for %s can't be decrypted here (%s); treating it as empty",
                           service_id, exc)
            return ""

    def set(self, service_id: str, password: str) -> None:
        with self._lock:
            data = self._load()
            if password:
                data[service_id] = base64.b64encode(_dpapi(password.encode("utf-8"), protect=True)).decode()
            else:
                data.pop(service_id, None)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)


def option_value(value: str) -> str:
    """Quote a value for a MySQL option file ([client] password="...")."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


@contextmanager
def client_option_file(port: int, password: str, user: str = "root",
                       host: str = "127.0.0.1") -> Iterator[Path]:
    """A temporary [client] option file for mysql / mysqladmin / mysqldump, deleted afterwards."""
    handle, name = tempfile.mkstemp(prefix="varsha-client-", suffix=".cnf")
    path = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write("[client]\n")
            stream.write(f"user={user}\n")
            stream.write(f"password={option_value(password)}\n")
            stream.write(f"host={host}\nport={port}\nprotocol=TCP\n")
        yield path
    finally:
        try:
            path.unlink()
        except OSError:
            logger.warning("Could not delete temporary option file %s", path)
