from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from conftest import REPO_ROOT
from test_config_engine import PE_X64, PE_X86, add_php, touch
from varsha.config_engine.engine import ConfigEngine
from varsha.devtools import DevTools, extract_wordpress, update_settings_file, write_wp_config
from varsha.errors import ConfigError, ServiceError
from varsha.paths import AppPaths
from varsha.php.config import PhpOverrides, validate_ini
from varsha.services.config import parse_services
from varsha.services.manager import ServiceManager
from varsha.settings import Settings
from varsha.sites.discovery import load_overrides, save_override

APACHE = {"id": "apache", "name": "Apache", "type": "apache", "exe": "bin/apache/*/bin/httpd.exe",
          "args": ["-f", "{config}"], "ports": [80, 443], "config_template": "httpd.conf.j2",
          "config_file": "etc/apache/httpd.conf"}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copytree(REPO_ROOT / "etc" / "templates", tmp_path / "etc" / "templates")
    AppPaths(tmp_path).ensure_dirs()
    touch(tmp_path / "bin/apache/httpd-2.4.68/bin/httpd.exe")
    add_php(tmp_path, "php-8.3.33-Win32-vs16-x64", PE_X64, ["mysqli", "mbstring", "intl", "opcache"])
    add_php(tmp_path, "php-5.4.33-nts-Win32-VC9-x86", PE_X86, ["mysql", "mysqli", "mbstring"], ext_subdir=False)
    return tmp_path


@pytest.fixture
def engine(root: Path) -> ConfigEngine:
    return ConfigEngine(AppPaths(root), Settings(), parse_services({"services": [APACHE]}))


@pytest.fixture
def tools(root: Path, engine: ConfigEngine) -> DevTools:
    services = parse_services({"services": [APACHE]})
    manager = ServiceManager(AppPaths(root), services, Settings(), locator=lambda _d: None)
    return DevTools(AppPaths(root), engine, manager)


# ---------------------------------------------------------------------- per-site PHP

def test_site_php_goes_into_its_vhost(root: Path, engine: ConfigEngine, tools: DevTools) -> None:
    (root / "www/legacy").mkdir(parents=True)
    (root / "www/modern").mkdir(parents=True)
    tools.set_site_php("legacy.test", "5.4")
    assert load_overrides(root / "config/sites.json")["legacy"].php == "5.4"
    engine.generate_sites(engine.services[0])
    legacy = (root / "etc/apache/sites-enabled/legacy.test.conf").read_text(encoding="utf-8")
    modern = (root / "etc/apache/sites-enabled/modern.test.conf").read_text(encoding="utf-8")
    assert f'FcgidWrapper "{(root / "bin/php/php-5.4.33-nts-Win32-VC9-x86/php-cgi.exe").as_posix()}" .php' in legacy
    assert "PHPRC" in legacy and "FcgidWrapper" not in modern  # modern uses the server-wide default
    tools.set_site_php("legacy.test", None)
    assert "legacy" not in load_overrides(root / "config/sites.json")


def test_unknown_site_php_is_refused(root: Path, tools: DevTools) -> None:
    (root / "www/shop").mkdir(parents=True)
    with pytest.raises(ServiceError, match="No PHP 7.4"):
        tools.set_site_php("shop.test", "7.4")


def test_save_override_keeps_other_keys(tmp_path: Path) -> None:
    path = tmp_path / "sites.json"
    path.write_text(json.dumps({"sites": {"shop": {"docroot": "web"}}, "_note": "kept"}), encoding="utf-8")
    save_override(path, "Shop", php="8.3")
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["sites"]["shop"] == {"docroot": "web", "php": "8.3"} and raw["_note"] == "kept"
    with pytest.raises(ConfigError):
        save_override(path, "shop", php="eight")


# ---------------------------------------------------------------------- extensions + php.ini

def test_extension_defaults_and_overrides(root: Path, engine: ConfigEngine) -> None:
    build = engine.php_build_by_folder("php-8.3.33-Win32-vs16-x64")
    exts = {e["name"]: e for e in engine.php_extensions(build)}
    assert exts["intl"]["enabled"] and not exts["opcache"]["enabled"] and exts["opcache"]["zend"]
    engine.php_config.save(build.folder.name, PhpOverrides({"intl": False, "opcache": True}, {"memory_limit": "1G"}))
    ini = engine.generate_php_ini(build).read_text(encoding="utf-8")
    assert "extension=php_intl.dll" not in ini
    assert "zend_extension=php_opcache.dll" in ini
    assert ini.rstrip().endswith("memory_limit = 1G")  # user settings come last, so they win
    legacy = engine.php_build_by_folder("php-5.4.33-nts-Win32-VC9-x86")
    assert "extension=php_mysql.dll" in engine.generate_php_ini(legacy).read_text(encoding="utf-8")


@pytest.mark.parametrize("ini", [{"extension_dir": "C:/x"}, {"bad key!": "1"}, {"memory_limit": "1G\nextension=evil.dll"}])
def test_locked_or_bad_ini_is_refused(ini: dict[str, str]) -> None:
    with pytest.raises(ConfigError):
        validate_ini(ini)


def test_save_php_config_stores_only_changes(root: Path, engine: ConfigEngine, tools: DevTools,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "check_php", lambda build: [])
    monkeypatch.setattr(tools.manager, "reload", lambda sid: "not running")
    folder = "php-8.3.33-Win32-vs16-x64"
    tools.save_php_config(folder, {"intl": True, "mysqli": False}, {"memory_limit": "512M"})
    stored = engine.php_config.get(folder)
    assert stored.extensions == {"mysqli": False}  # intl=True is already the default
    with pytest.raises(ServiceError, match="no extension"):
        tools.save_php_config(folder, {"xdebug": True}, {})


def test_update_settings_file_keeps_other_keys(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"tld": "test", "default_php": "8.3"}), encoding="utf-8")
    update_settings_file(path, default_php="8.5.12")
    assert json.loads(path.read_text(encoding="utf-8")) == {"tld": "test", "default_php": "8.5.12"}


# ---------------------------------------------------------------------- Quick Create

def test_create_blank_site(root: Path, tools: DevTools) -> None:
    message = tools.create("blank", "shop")
    assert "shop.test created" in message
    assert "<?= PHP_VERSION ?>" in (root / "www/shop/public/index.php").read_text(encoding="utf-8")
    with pytest.raises(ServiceError, match="already exists"):
        tools.create("blank", "shop")


@pytest.mark.parametrize("name", ["my shop", "../evil", "", "-x", "shop_1"])
def test_create_rejects_bad_names(tools: DevTools, name: str) -> None:
    with pytest.raises(ServiceError):
        tools.create("blank", name)


def test_create_lowercases_names(root: Path, tools: DevTools) -> None:
    tools.create("blank", "Shop")
    assert (root / "www/shop/public/index.php").is_file()


def test_create_db_requires_running_database(root: Path, tools: DevTools) -> None:
    with pytest.raises(ServiceError):
        tools.create("blank", "shop", db_service="apache")


def _zip(entries: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def test_wordpress_extract_and_config(tmp_path: Path) -> None:
    sample = ("<?php\ndefine( 'DB_NAME', 'database_name_here' );\ndefine( 'DB_USER', 'username_here' );\n"
              "define( 'DB_PASSWORD', 'password_here' );\ndefine( 'DB_HOST', 'localhost' );\n"
              "define( 'AUTH_KEY', 'put your unique phrase here' );\ndefine( 'NONCE_SALT', 'put your unique phrase here' );\n")
    data = _zip({"wordpress/index.php": "<?php // wp", "wordpress/wp-config-sample.php": sample,
                 "wordpress/wp-admin/admin.php": "x"})
    site = tmp_path / "blog"
    extract_wordpress(data, site)
    assert (site / "index.php").is_file() and (site / "wp-admin/admin.php").is_file()
    write_wp_config(site, {"name": "blog", "user": "root", "password": "p'w", "host": "127.0.0.1", "port": 3307})
    config = (site / "wp-config.php").read_text(encoding="utf-8")
    assert "define( 'DB_NAME', 'blog' );" in config and "define( 'DB_HOST', '127.0.0.1:3307' );" in config
    assert "define( 'DB_PASSWORD', 'p\\'w' );" in config
    assert "put your unique phrase here" not in config
    salts = [line for line in config.splitlines() if "KEY" in line or "SALT" in line]
    assert len({line.split(",", 1)[1] for line in salts}) == 2  # every salt is different


def test_wordpress_zip_slip_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ServiceError, match="Unsafe path"):
        extract_wordpress(_zip({"wordpress/../../evil.txt": "x"}), tmp_path / "blog")


# ---------------------------------------------------------------------- terminal

def test_terminal_script_puts_tools_on_path(root: Path, engine: ConfigEngine, tools: DevTools) -> None:
    build = engine.php_build_by_folder("php-5.4.33-nts-Win32-VC9-x86")
    script = tools.terminal_script(build, "legacy.test").read_text(encoding="utf-8")
    assert str(build.folder) in script and "etc\\shims" in script.replace("/", "\\") and "PHP 5.4.33" in script
    composer = (root / "etc/shims/composer.cmd").read_text(encoding="utf-8")
    assert str(build.php_exe) in composer and "composer.phar" in composer


def test_delete_only_inside_www(root: Path, tools: DevTools) -> None:
    with pytest.raises(ServiceError, match="No site"):
        tools.delete_site("nope.test")


# ---------------------------------------------------------------------- Link a folder

@pytest.fixture
def project(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A project outside the varsha root, like D:\\Work\\...\\ReportDesk."""
    folder = tmp_path_factory.mktemp("projects") / "ReportDesk"
    (folder / "public").mkdir(parents=True)
    (folder / "public" / "index.php").write_text("<?php echo 'rd';", encoding="utf-8")
    (folder / "src.php").write_text("<?php // code", encoding="utf-8")
    return folder


def test_link_a_folder_creates_a_junction(root: Path, tools: DevTools, engine: ConfigEngine, project: Path) -> None:
    message = tools.create("link", "rdesk", source=str(project), php="5.4")
    link = root / "www" / "rdesk"
    assert link.is_junction() and "linked to" in message
    assert (link / "public" / "index.php").read_text(encoding="utf-8") == "<?php echo 'rd';"
    site = next(s for s in engine.sites() if s.domain == "rdesk.test")
    assert site.docroot.name == "public"  # its public\ is the web root
    assert json.loads((root / "config/sites.json").read_text(encoding="utf-8"))["sites"]["rdesk"]["php"] == "5.4"
    assert sorted(p.name for p in project.iterdir()) == ["public", "src.php"]  # nothing written into the project


def test_deleting_a_linked_site_removes_only_the_link(root: Path, tools: DevTools, project: Path) -> None:
    tools.create("link", "rdesk", source=str(project))
    message = tools.delete_site("rdesk.test")
    assert "unlinked" in message and str(project) in message
    assert not (root / "www" / "rdesk").exists() and not (root / "www" / "rdesk").is_junction()
    assert (project / "public" / "index.php").is_file() and (project / "src.php").is_file()  # untouched


@pytest.mark.parametrize("bad", ["", "relative\\folder", "missing"])
def test_link_source_must_be_an_existing_absolute_folder(root: Path, tools: DevTools, project: Path, bad: str) -> None:
    source = str(project.parent / "missing") if bad == "missing" else bad
    with pytest.raises(ServiceError):
        tools.create("link", "rdesk", source=source)
    assert not (root / "www" / "rdesk").exists()


def test_link_refuses_varsha_itself_drives_and_files(root: Path, tools: DevTools, project: Path) -> None:
    for source in (root, root / "www", root / "data", Path(root.anchor)):
        with pytest.raises(ServiceError):
            tools.link_source(str(source))
    with pytest.raises(ServiceError, match="isn't a folder"):
        tools.link_source(str(project / "src.php"))
    assert tools.link_source(f'"{project}"') == project.resolve()  # pasted "Copy as path" quotes are fine


def test_laravel_is_gone(tools: DevTools) -> None:
    with pytest.raises(ServiceError, match="Unknown site type"):
        tools.create("laravel", "shop")


def test_vs_code_opens_the_real_project_folder(root: Path, tools: DevTools, tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    import _winapi
    import subprocess as sp
    project = tmp_path / "projects" / "admin"
    (project / "public").mkdir(parents=True)
    _winapi.CreateJunction(str(project), str(root / "www" / "admin"))  # a "Link a folder" site
    (root / "www" / "shop").mkdir(parents=True)  # a normal one
    assert tools.project_folder("admin.test") == project  # the junction's target, not www\admin
    assert tools.project_folder("shop.test") == root / "www" / "shop"
    launched: list[list[str]] = []
    monkeypatch.setattr(sp, "Popen", lambda cmd, **kw: launched.append(cmd))
    code = tmp_path / "Code.exe"
    code.write_bytes(b"")
    monkeypatch.setattr(DevTools, "find_vscode", staticmethod(lambda: code))
    assert str(project) in tools.open_vscode("admin.test")
    assert launched == [[str(code), str(project)]]
    monkeypatch.setattr(DevTools, "find_vscode", staticmethod(lambda: None))
    with pytest.raises(ServiceError, match="VS Code isn't installed"):
        tools.open_vscode("admin.test")
    (root / "www" / "admin").rmdir()  # only the junction goes
    assert project.is_dir()
