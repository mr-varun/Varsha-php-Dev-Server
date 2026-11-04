from __future__ import annotations

from pathlib import Path

import pytest

from conftest import REPO_ROOT
from varsha.errors import ConfigError
from varsha.settings import Settings, load_settings


def test_shipped_settings() -> None:
    settings = load_settings(REPO_ROOT / "config" / "settings.json")
    assert settings.max_auto_restarts == 3
    assert settings.log_max_bytes == 10 * 1024 * 1024


def test_missing_file_gives_defaults(tmp_path: Path) -> None:
    assert load_settings(tmp_path / "settings.json") == Settings()


@pytest.mark.parametrize("body", ['{"stop_timeout_seconds": -1}', '{"ask_before_exit": "yes"}',
                                  '{"max_auto_restarts": 1.5}', "[1]", "{"])
def test_invalid(tmp_path: Path, body: str) -> None:
    path = tmp_path / "settings.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(path)


def test_unknown_keys_are_ignored(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text('{"tld": "test", "stop_timeout_seconds": 3}', encoding="utf-8")
    assert load_settings(path).stop_timeout == 3.0
