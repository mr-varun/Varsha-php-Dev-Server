"""Locate the varsha root folder and derive every other path from it.

Portability rule: no other module builds an absolute path on its own. Everything hangs off
`AppPaths.root`, so copying the whole folder to another drive keeps working.
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT_ENV_VAR = "VARSHA_ROOT"
DEFAULT_CONFIGS = ("services.json", "settings.json")


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
