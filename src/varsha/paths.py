"""Locate the varsha root folder and derive every other path from it.

Portability rule: no other module builds an absolute path on its own. Everything hangs off
`AppPaths.root`, so copying the whole folder to another drive keeps working.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT_ENV_VAR = "VARSHA_ROOT"
DEFAULT_CONFIGS = ("services.json", "settings.json")
START_PAGE = "start-page.php"


def bundle_dir() -> Path:
    """Read-only files shipped with the code (templates, default config): the PyInstaller
    folder (`_internal`) when frozen, else the source checkout."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parents[2]


def find_root() -> Path:
    """Return the varsha root folder.

    Order: the VARSHA_ROOT environment variable (tests, dev), then the folder of the frozen
    varsha.exe (PyInstaller), then the source checkout (<root>/src/varsha/paths.py).
    """
    override = os.environ.get(ROOT_ENV_VAR)
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AppPaths:
    root: Path

    @classmethod
    def discover(cls) -> AppPaths:
        return cls(find_root())

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def etc(self) -> Path:
        return self.root / "etc"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def www(self) -> Path:
        return self.root / "www"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def templates(self) -> Path:
        """<root>\\etc\\templates if that folder exists (source checkout, tests), else the copy
        bundled into Varsha.exe."""
        own = self.etc / "templates"
        return own if own.is_dir() else bundle_dir() / "etc" / "templates"

    @property
    def services_file(self) -> Path:
        return self.config / "services.json"

    @property
    def settings_file(self) -> Path:
        return self.config / "settings.json"

    @property
    def app_log(self) -> Path:
        return self.logs / "varsha.log"

    def service_log(self, service_id: str) -> Path:
        return self.logs / f"{service_id}.log"

    def ensure_dirs(self) -> None:
        for folder in (self.config, self.bin, self.etc, self.data, self.www, self.logs):
            folder.mkdir(parents=True, exist_ok=True)

    def seed_config(self) -> None:
        """First start of a fresh folder: copy the default services.json/settings.json from the
        bundle. Existing files are never touched (they're the live settings)."""
        for name in DEFAULT_CONFIGS:
            dest, source = self.config / name, bundle_dir() / "config" / name
            if not dest.exists() and source.is_file() and source.resolve() != dest.resolve():
                shutil.copy2(source, dest)

    def seed_start_page(self) -> str:
        """www\\index.php (the http://localhost/ start page) comes from etc\\templates\\start-page.php.

        Created when missing, and updated to a newer version only while it is still exactly the
        copy varsha put there last time (its SHA-256 is kept in data\\start-page.sha256). A page the
        user edited, or their own index.php, is never touched. Returns what happened."""
        source = self.templates / START_PAGE
        dest, stamp = self.www / "index.php", self.data / "start-page.sha256"
        if not source.is_file():
            return "no-template"
        new = source.read_bytes()
        new_hash = hashlib.sha256(new).hexdigest()
        try:
            last = stamp.read_text(encoding="ascii").strip()
        except OSError:
            last = ""
        if dest.exists():
            current = hashlib.sha256(dest.read_bytes()).hexdigest()
            if current == new_hash:
                result = "current"
            elif current != last:
                return "kept"  # the user's own page (or edited copy)
            else:
                result = "updated"
        else:
            result = "created"
        if result != "current":
            self.www.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_name("index.php.varsha-new")
            tmp.write_bytes(new)
            os.replace(tmp, dest)
        if last != new_hash:
            self.data.mkdir(parents=True, exist_ok=True)
            stamp.write_text(new_hash, encoding="ascii")
        return result
