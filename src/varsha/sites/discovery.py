"""Turn the folders in www\\ into sites, applying config/sites.json overrides.

    www\\shop\\           -> shop.test, docroot www\\shop\\public if it exists, else www\\shop
    www\\My_Blog\\        -> my-blog.test (host names allow only a-z, 0-9 and "-")

config/sites.json (all keys optional):
    {"sites": {"shop": {"docroot": "web", "enabled": true}}}
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any

from varsha.errors import ConfigError

logger = logging.getLogger(__name__)

RESERVED_TLDS = frozenset({"dev", "app", "com", "net", "org", "local"})  # .dev/.app are real TLDs with HSTS
TLD_RE = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$|^[a-z]$")
LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


PHP_VERSION_RE = re.compile(r"^\d+(\.\d+){0,2}$")


@dataclass(frozen=True)
class SiteOverride:
    docroot: str | None = None  # relative to the site folder
    enabled: bool = True
    php: str | None = None  # version prefix, e.g. "5.4" or "8.5.12"; None = the default PHP


@dataclass(frozen=True)
class Site:
    name: str  # DNS label, e.g. "my-blog"
    domain: str  # "my-blog.test"
    folder: Path
    docroot: Path
    php: str | None = None

    @property
    def aliases(self) -> tuple[str, ...]:
        return (f"*.{self.domain}",)

    @property
    def hosts_names(self) -> tuple[str, ...]:
        """Names for the hosts file, which can't hold wildcards."""
        return (self.domain, f"www.{self.domain}")


def validate_tld(tld: str) -> str:
    tld = tld.strip().lstrip(".").lower()
    if not TLD_RE.match(tld):
        raise ConfigError(f'Invalid TLD "{tld}": use letters, digits and "-" only, e.g. "test"')
    if tld in RESERVED_TLDS:
        raise ConfigError(
            f'".{tld}" is a real top-level domain (and .dev/.app force HTTPS in browsers). '
            'Use "test", which is reserved for exactly this.')
    return tld


def slugify(folder_name: str) -> str | None:
    """Folder name -> DNS label, or None if nothing usable is left."""
    label = re.sub(r"[^a-z0-9-]+", "-", folder_name.lower()).strip("-")
    label = re.sub(r"-{2,}", "-", label)[:63].strip("-")
    return label if label and LABEL_RE.match(label) else None


def load_overrides(path: Path) -> dict[str, SiteOverride]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path.name} is not valid JSON (line {exc.lineno}): {exc.msg}") from exc
    sites = raw.get("sites", {}) if isinstance(raw, dict) else None
    if not isinstance(sites, dict):
        raise ConfigError(f'{path.name}: expected {{"sites": {{"<folder>": {{...}}}}}}')
    overrides: dict[str, SiteOverride] = {}
    for name, value in sites.items():
        if str(name).startswith("_"):
            continue
        if not isinstance(value, dict):
            raise ConfigError(f'{path.name}: site "{name}" must be an object')
        overrides[str(name).lower()] = _override(name, value, path.name)
    return overrides


def _override(name: str, value: dict[str, Any], source: str) -> SiteOverride:
    docroot = value.get("docroot")
    if docroot is not None:
        if not isinstance(docroot, str) or not docroot.strip():
            raise ConfigError(f'{source}: "{name}".docroot must be a folder name')
        pure = PureWindowsPath(docroot)
        if pure.drive or pure.root or ".." in pure.parts:
            raise ConfigError(f'{source}: "{name}".docroot must be inside the site folder')
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError(f'{source}: "{name}".enabled must be true or false')
    php = value.get("php")
    if php is not None and (not isinstance(php, str) or not PHP_VERSION_RE.match(php)):
        raise ConfigError(f'{source}: "{name}".php must be a version like "8.3" or "5.4.33"')
    return SiteOverride(docroot=docroot, enabled=enabled, php=php)


def save_override(path: Path, folder_name: str, **changes: Any) -> None:
    """Set (or with None, remove) keys of one site's entry in sites.json, keeping the rest."""
    raw: dict[str, Any] = {}
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path.name} is not valid JSON (line {exc.lineno}): {exc.msg}") from exc
    sites = raw.setdefault("sites", {})
    entry = sites.setdefault(folder_name.lower(), {})
    for key, value in changes.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    if not entry:
        sites.pop(folder_name.lower(), None)
    _override(folder_name, sites.get(folder_name.lower(), {}), path.name)  # validate before writing
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def discover_sites(www: Path, tld: str, overrides: dict[str, SiteOverride] | None = None) -> list[Site]:
    """Sites for the folders directly inside www, sorted by domain. Hidden folders are skipped."""
    tld = validate_tld(tld)
    overrides = overrides or {}
    if not www.is_dir():
        return []
    sites: dict[str, Site] = {}
    for folder in sorted(www.iterdir(), key=lambda p: p.name.lower()):
        if not folder.is_dir() or folder.name.startswith((".", "_")):
            continue
        override = overrides.get(folder.name.lower(), SiteOverride())
        if not override.enabled:
            continue
        name = slugify(folder.name)
        if name is None:
            logger.warning("Skipping www\\%s: no usable host name in the folder name", folder.name)
            continue
        if name in sites:
            logger.warning("Skipping www\\%s: %s.%s is already used by www\\%s",
                           folder.name, name, tld, sites[name].folder.name)
            continue
        if override.docroot:
            docroot = folder / override.docroot
        elif (folder / "public").is_dir():  # Laravel, Symfony, most modern frameworks
            docroot = folder / "public"
        else:
            docroot = folder
        sites[name] = Site(name=name, domain=f"{name}.{tld}", folder=folder, docroot=docroot, php=override.php)
    return sorted(sites.values(), key=lambda s: s.domain)
