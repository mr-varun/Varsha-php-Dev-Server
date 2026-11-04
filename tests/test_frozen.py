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


def test_start_page_created_updated_and_user_edits_kept(tmp_path):
    root = tmp_path / "root"
    (root / "etc" / "templates").mkdir(parents=True)
    template = root / "etc" / "templates" / paths_mod.START_PAGE
    template.write_text("<?php // v1", encoding="utf-8")
    paths = AppPaths(root)
    page = paths.www / "index.php"
    assert paths.seed_start_page() == "created" and page.read_text(encoding="utf-8") == "<?php // v1"
    assert paths.seed_start_page() == "current"
    template.write_text("<?php // v2", encoding="utf-8")
    assert paths.seed_start_page() == "updated" and page.read_text(encoding="utf-8") == "<?php // v2"
    page.write_text("<?php // my edit", encoding="utf-8")
    template.write_text("<?php // v3", encoding="utf-8")
    assert paths.seed_start_page() == "kept" and page.read_text(encoding="utf-8") == "<?php // my edit"


def test_existing_start_page_is_adopted_or_kept(tmp_path):
    paths = AppPaths(tmp_path)  # no own templates: uses the bundled start page
    paths.www.mkdir(parents=True)
    page = paths.www / "index.php"
    page.write_bytes((REPO / "etc" / "templates" / paths_mod.START_PAGE).read_bytes())
    assert paths.seed_start_page() == "current"  # an install from before: recorded, so later versions update it
    assert (paths.data / "start-page.sha256").is_file()
    other = AppPaths(tmp_path / "other")
    other.www.mkdir(parents=True)
    (other.www / "index.php").write_text("<?php echo 'mine';", encoding="utf-8")
    assert other.seed_start_page() == "kept"


def test_frozen_commands_use_run_module(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    command = elevate.megha_command(AppPaths(tmp_path))
    assert command[1:] == ["--run-module", "varsha.helper.megha"]
