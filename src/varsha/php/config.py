"""Per-PHP-build customisations: extension on/off and php.ini directives.

Stored in config/php.json, keyed by the build's folder name:

    {"php-8.3.33-Win32-vs16-x64": {"extensions": {"intl": false, "opcache": true},
                                   "ini": {"memory_limit": "1G"}}}

The generated php.ini (etc/php/<folder>/php.ini) is rebuilt from the template plus these
values, so edits survive regeneration and never need hand-editing generated files.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from varsha.errors import ConfigError

INI_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,79}$")
EXT_RE = re.compile(r"^[a-z0-9_]{1,40}$")
# Directives that would break varsha's own wiring if changed from the panel.
LOCKED_KEYS = frozenset({"extension_dir", "extension", "zend_extension", "error_log", "session.save_path",
                         "upload_tmp_dir", "sys_temp_dir"})


@dataclass(frozen=True)
class PhpOverrides:
    extensions: dict[str, bool] = field(default_factory=dict)
    ini: dict[str, str] = field(default_factory=dict)


def validate_ini(values: Any) -> dict[str, str]:
    if not isinstance(values, dict):
        raise ConfigError("ini must be an object of directive: value")
    clean: dict[str, str] = {}
    for key, value in values.items():
        key = str(key).strip()
        if not INI_KEY_RE.match(key):
            raise ConfigError(f'"{key}" is not a php.ini directive name')
        if key.lower() in LOCKED_KEYS:
            raise ConfigError(f'"{key}" is managed by varsha and can\'t be changed here')
        text = str(value).strip()
        if "\n" in text or "\r" in text or len(text) > 400:
            raise ConfigError(f'The value of "{key}" must be one line')
        clean[key] = text
    return clean


def validate_extensions(values: Any) -> dict[str, bool]:
    if not isinstance(values, dict):
        raise ConfigError("extensions must be an object of name: true/false")
    clean: dict[str, bool] = {}
    for name, enabled in values.items():
        if not EXT_RE.match(str(name)) or not isinstance(enabled, bool):
            raise ConfigError(f'Invalid extension setting "{name}"')
        clean[str(name)] = enabled
    return clean


class PhpConfigStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, PhpOverrides]:
        if not self.path.is_file():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"{self.path.name} is not valid JSON: {exc}") from exc
        result: dict[str, PhpOverrides] = {}
        for folder, value in (raw.items() if isinstance(raw, dict) else []):
            if folder.startswith("_") or not isinstance(value, dict):
                continue
            result[folder] = PhpOverrides(validate_extensions(value.get("extensions", {})),
                                          validate_ini(value.get("ini", {})))
        return result

    def get(self, folder: str) -> PhpOverrides:
        return self.load().get(folder, PhpOverrides())

    def save(self, folder: str, overrides: PhpOverrides) -> None:
        data = {k: {"extensions": v.extensions, "ini": v.ini} for k, v in self.load().items()}
        data[folder] = {"extensions": overrides.extensions, "ini": overrides.ini}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)
