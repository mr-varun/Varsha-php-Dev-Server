"""The frozen Varsha.exe paths: --run-module, bundled templates, seeded config."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from varsha import paths as paths_mod
from varsha.helper import elevate
from varsha.paths import AppPaths

REPO = Path(__file__).resolve().parents[1]


def run_main(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-m", "varsha", *args], capture_output=True, text=True,
                          cwd=str(REPO), env={**__import__("os").environ, "PYTHONPATH": str(REPO / "src")}, timeout=60)


def test_run_module_rejects_foreign_modules():
    for bad in ("os", "varsha", "varsha..x", "varsha.x;y"):
        result = run_main("--run-module", bad)
        assert result.returncode == 2, bad


def test_run_module_runs_a_varsha_module_as_main():
    result = run_main("--run-module", "varsha.helper.megha", "--help")
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()


def test_version_prints_root():
    result = run_main("--version")
    assert result.returncode == 0
    assert result.stdout.startswith("varsha ")


def test_templates_fall_back_to_the_bundle(tmp_path):
    paths = AppPaths(tmp_path)
    assert paths.templates == REPO / "etc" / "templates"
    (tmp_path / "etc" / "templates").mkdir(parents=True)
    assert paths.templates == tmp_path / "etc" / "templates"


def test_seed_config_copies_defaults_once(tmp_path):
    paths = AppPaths(tmp_path)
    paths.ensure_dirs()
    paths.seed_config()
    for name in paths_mod.DEFAULT_CONFIGS:
        assert (paths.config / name).read_bytes() == (REPO / "config" / name).read_bytes()
    paths.settings_file.write_text("{}", encoding="utf-8")
    paths.seed_config()
    assert paths.settings_file.read_text(encoding="utf-8") == "{}"  # live settings are never overwritten


def test_frozen_commands_use_run_module(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    command = elevate.megha_command(AppPaths(tmp_path))
    assert command[1:] == ["--run-module", "varsha.helper.megha"]
