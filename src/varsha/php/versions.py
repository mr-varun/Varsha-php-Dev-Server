"""Find every PHP build under bin\\php\\. Any number of versions can sit side by side."""

from __future__ import annotations

import logging
import re
import struct
from dataclasses import dataclass
from pathlib import Path

from varsha.services.resolve import detect_version, version_key

logger = logging.getLogger(__name__)

_MACHINES = {0x8664: "x64", 0x014C: "x86", 0xAA64: "arm64"}
_TS_DLL = re.compile(r"^php\d+ts\.dll$", re.IGNORECASE)


@dataclass(frozen=True)
class PhpBuild:
    version: str
    folder: Path
    php_exe: Path
    cgi_exe: Path | None  # php-cgi.exe, needed for per-site PHP via mod_fcgid
    thread_safe: bool
    arch: str | None

    @property
    def label(self) -> str:
        parts = [self.version, "TS" if self.thread_safe else "NTS"]
        if self.arch:
            parts.append(self.arch)
        return " ".join(parts)


def pe_arch(exe: Path) -> str | None:
    """CPU architecture of a Windows .exe, read from its PE header."""
    try:
        with open(exe, "rb") as handle:
            header = handle.read(4096)
        offset = struct.unpack_from("<I", header, 0x3C)[0]
        if header[offset:offset + 4] != b"PE\0\0":
            return None
        return _MACHINES.get(struct.unpack_from("<H", header, offset + 4)[0])
    except (OSError, struct.error):
        return None


def find_php_builds(php_root: Path, root: Path) -> list[PhpBuild]:
    """All PHP builds in bin\\php\\, newest first. Folders without a version are skipped."""
    if not php_root.is_dir():
        return []
    builds: list[PhpBuild] = []
    for folder in php_root.iterdir():
        php_exe = folder / "php.exe"
        if not php_exe.is_file():
            continue
        version = detect_version(php_exe, root)
        if version is None:
            logger.warning("Skipping %s: no version number in the folder name", folder.name)
            continue
        cgi = folder / "php-cgi.exe"
        # Thread-safe builds ship php8ts.dll / php7ts.dll / php5ts.dll; NTS ships php8.dll.
        # (A plain "php*ts.dll" glob would also match the extension php_sockets.dll.)
        thread_safe = any(_TS_DLL.match(p.name) for p in folder.glob("php*.dll"))
        builds.append(PhpBuild(version, folder, php_exe, cgi if cgi.is_file() else None,
                               thread_safe, pe_arch(php_exe)))
    builds.sort(key=lambda b: version_key(b.version), reverse=True)
    return builds
