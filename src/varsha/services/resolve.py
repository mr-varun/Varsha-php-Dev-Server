"""Find service binaries under bin\\, detect their versions, and expand {placeholders}."""

from __future__ import annotations

import re
import string
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath

from varsha.errors import ConfigError
from varsha.paths import AppPaths
from varsha.services.models import Installation, ServiceDef

VERSION_RE = re.compile(r"(\d+(?:\.\d+)+)")

PLACEHOLDERS = frozenset(
    {"root", "bin", "etc", "data", "www", "logs", "exe", "exe_dir", "home",
     "config", "data_dir", "port", "ports", "id", "version"}
)


def version_key(version: str | None) -> tuple[int, ...]:
    if not version:
        return ()
    return tuple(int(part) for part in version.split("."))


def detect_version(exe: Path, root: Path) -> str | None:
    """Return the first version-looking folder name on the way down to `exe`.

    bin/apache/httpd-2.4.68-260617-Win64-VS18/bin/httpd.exe -> "2.4.68"
    bin/php/php-8.5.12-1-nts-Win32-vs17-x64/php.exe          -> "8.5.12"
    bin/mailpit/mailpit.exe                                  -> None
    """
    try:
        parts = exe.relative_to(root).parts[:-1]
    except ValueError:
        parts = exe.parts[:-1]
    for part in parts:
        match = VERSION_RE.search(part)
        if match:
            return match.group(1)
    return None


def find_installations(root: Path, pattern: str) -> list[Installation]:
    """All binaries matching `pattern` under `root`, newest version first."""
    # Path.glob is case-insensitive on Windows, so "HTTPD.EXE" on disk still matches.
    found = [
        Installation(path, detect_version(path, root))
        for path in root.glob(pattern.replace("\\", "/"))
        if path.is_file()
    ]
    found.sort(key=lambda inst: version_key(inst.version), reverse=True)
    return found


def find_installation(root: Path, defn: ServiceDef) -> Installation | None:
    if not defn.exe:
        return None
    matches = find_installations(root, defn.exe)
    return matches[0] if matches else None


def install_hint(pattern: str) -> str:
    """Human-readable "where to put the binary" for a missing service.

    "bin/apache/*/bin/httpd.exe"           -> bin\\apache\\<version folder>\\bin\\httpd.exe
    "bin/mysql/mysql-8.4*/bin/mysqld.exe"  -> bin\\mysql\\mysql-8.4...\\bin\\mysqld.exe
    """
    parts = []
    for part in PureWindowsPath(pattern).parts:
        parts.append("<version folder>" if part == "*" else part.replace("*", "..."))
    return "\\".join(parts)


def template_fields(template: str) -> set[str]:
    """Placeholder base names used in a template string ("{ports[1]}" -> "ports")."""
    names: set[str] = set()
    for _literal, field_name, _spec, _conv in string.Formatter().parse(template):
        if field_name is None:
            continue
        names.add(re.split(r"[.\[]", field_name, maxsplit=1)[0])
    return names


def build_context(paths: AppPaths, defn: ServiceDef, inst: Installation) -> dict[str, object]:
    """Values for {placeholders}.

    Everything is a forward-slash absolute path: MySQL option files treat "\\" as an escape
    character and Apache prefers "/", while Windows APIs accept both.
    """
    exe_dir = inst.exe.parent
    # {home}: the folder above bin\ - Apache's ServerRoot, MySQL's basedir.
    home = exe_dir.parent if exe_dir.name.lower() == "bin" else exe_dir
    ctx: dict[str, object] = {
        "root": paths.root.as_posix(),
        "bin": paths.bin.as_posix(),
        "etc": paths.etc.as_posix(),
        "data": paths.data.as_posix(),
        "www": paths.www.as_posix(),
        "logs": paths.logs.as_posix(),
        "exe": inst.exe.as_posix(),
        "exe_dir": exe_dir.as_posix(),
        "home": home.as_posix(),
        "ports": list(defn.ports),
        "port": defn.ports[0] if defn.ports else "",
        "id": defn.id,
        "version": inst.version or "",
    }
    if defn.config_file:
        ctx["config"] = (paths.root / defn.config_file).as_posix()
    if defn.data_dir:
        ctx["data_dir"] = (paths.root / defn.data_dir).as_posix()
    return ctx


def expand(templates: Sequence[str], ctx: dict[str, object], service_id: str) -> list[str]:
    try:
        return [template.format_map(ctx) for template in templates]
    except (KeyError, IndexError, ValueError) as exc:
        raise ConfigError(f"Service '{service_id}': bad placeholder in {list(templates)}: {exc}") from exc
