from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from conftest import REPO_ROOT
from varsha.config_engine.engine import ConfigEngine, php_str, write_if_changed
from varsha.paths import AppPaths
from varsha.services.config import parse_services
from varsha.services.models import Installation
from varsha.settings import ExtraDbServer, Settings

PE_X64 = 0x8664
PE_X86 = 0x014C


def touch(path: Path, content: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def fake_pe(machine: int) -> bytes:
    header = bytearray(512)
    header[0:2] = b"MZ"
    header[0x3C:0x40] = (0x80).to_bytes(4, "little")
    header[0x80:0x84] = b"PE\0\0"
    header[0x84:0x86] = machine.to_bytes(2, "little")
    return bytes(header)


def add_php(root: Path, folder: str, machine: int, exts: list[str], ext_subdir: bool = True) -> Path:
    base = root / "bin" / "php" / folder
    touch(base / "php.exe", fake_pe(machine))
    touch(base / "php-cgi.exe")
    for ext in exts:
        touch((base / "ext" if ext_subdir else base) / f"php_{ext}.dll")
    if ext_subdir:
        (base / "ext").mkdir(parents=True, exist_ok=True)
    return base


SERVICES = {
    "services": [
        {"id": "apache", "name": "Apache 2.4", "type": "apache", "exe": "bin/apache/*/bin/httpd.exe",
         "args": ["-d", "{home}", "-f", "{config}"], "ports": [80], "config_template": "httpd.conf.j2",
         "config_file": "etc/apache/httpd.conf"},
        {"id": "mysql-8.4", "name": "MySQL 8.4", "type": "mysql", "exe": "bin/mysql/mysql-8.4*/bin/mysqld.exe",
         "ports": [3308], "config_template": "my.ini.j2", "config_file": "etc/mysql-8.4/my.ini",
         "data_dir": "data/mysql-8.4", "tools": {"client": "{exe_dir}/mysql.exe"}},
        {"id": "mysql-5.6", "name": "MySQL 5.6 (32-bit)", "type": "mysql",
         "exe": "bin/mysql/mysql-5.6*/bin/mysqld.exe", "ports": [3306], "config_template": "my.ini.j2",
         "config_file": "etc/mysql-5.6/my.ini", "data_dir": "data/mysql-5.6",
         "tools": {"client": "{exe_dir}/mysql.exe"}},
        {"id": "mariadb-11.8", "name": "MariaDB 11.8", "type": "mariadb",
         "exe": "bin/mariadb/mariadb-11.8*/bin/mariadbd.exe", "ports": [3307], "config_template": "my.ini.j2",
         "config_file": "etc/mariadb-11.8/my.ini", "data_dir": "data/mariadb-11.8",
         "tools": {"client": "{exe_dir}/mariadb.exe"}},
        {"id": "mailpit", "name": "Mailpit", "type": "mail", "exe": "bin/mailpit/mailpit.exe", "ports": [1125, 8125]},
    ]
}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copytree(REPO_ROOT / "etc" / "templates", tmp_path / "etc" / "templates")
    AppPaths(tmp_path).ensure_dirs()
    apache = tmp_path / "bin/apache/httpd-2.4.68/bin/httpd.exe"
    touch(apache)
    for module in ("mod_dir.so", "mod_alias.so", "mod_authz_core.so", "mod_fcgid.so"):
        touch(apache.parent.parent / "modules" / module)
    touch(tmp_path / "bin/mysql/mysql-8.4.3-winx64/bin/mysqld.exe")
    touch(tmp_path / "bin/mysql/mysql-5.6.12-win32/bin/mysqld.exe")
    touch(tmp_path / "bin/mariadb/mariadb-11.8.9-winx64/bin/mariadbd.exe")
    touch(tmp_path / "bin/phpmyadmin/index.php")
    add_php(tmp_path, "php-8.3.33-Win32-vs16-x64", PE_X64, ["mysqli", "mbstring", "gd", "openssl"])
    add_php(tmp_path, "php-8.5.12-nts-Win32-vs17-x64", PE_X64, ["mysqli"])
    add_php(tmp_path, "php-5.4.33-nts-Win32-VC9-x86", PE_X86, ["mysqli", "gd2", "exif", "mbstring"], ext_subdir=False)
    return tmp_path


def engine(root: Path, **settings: object) -> ConfigEngine:
    return ConfigEngine(AppPaths(root), Settings(**settings), parse_services(SERVICES))  # type: ignore[arg-type]


def inst(root: Path, relative: str, version: str) -> Installation:
    return Installation(root / relative, version)


def test_default_php_follows_setting(root: Path) -> None:
    assert engine(root).default_php().version == "8.3.33"  # type: ignore[union-attr]
    assert engine(root, default_php="8.5").default_php().version == "8.5.12"  # type: ignore[union-attr]
    # unknown prefix -> newest usable build
    assert engine(root, default_php="7.4").default_php().version == "8.5.12"  # type: ignore[union-attr]


def test_php_ini_per_build(root: Path) -> None:
    e = engine(root, php_timezone="Asia/Kolkata")
    e.generate_php_inis()
    ini83 = (root / "etc/php/php-8.3.33-Win32-vs16-x64/php.ini").read_text(encoding="utf-8")
    assert f'extension_dir = "{root.as_posix()}/bin/php/php-8.3.33-Win32-vs16-x64/ext"' in ini83
    assert "extension=php_mbstring.dll" in ini83 and "extension=php_gd.dll" in ini83
    assert "php_curl.dll" not in ini83  # not present in that build -> not enabled
    assert 'date.timezone = "Asia/Kolkata"' in ini83 and "smtp_port = 1125" in ini83
    assert "short_open_tag = Off" in ini83

    ini54 = (root / "etc/php/php-5.4.33-nts-Win32-VC9-x86/php.ini").read_text(encoding="utf-8")
    # PHP Desktop layout: extensions next to php.exe
    assert f'extension_dir = "{root.as_posix()}/bin/php/php-5.4.33-nts-Win32-VC9-x86"' in ini54
    assert ini54.index("php_mbstring.dll") < ini54.index("php_exif.dll")  # load order matters on PHP 5
    assert "short_open_tag = On" in ini54 and "sys_temp_dir" not in ini54


def test_httpd_conf(root: Path) -> None:
    e = engine(root)
    defn = next(d for d in e.services if d.id == "apache")
    e.prepare(defn, inst(root, "bin/apache/httpd-2.4.68/bin/httpd.exe", "2.4.68"))
    conf = (root / "etc/apache/httpd.conf").read_text(encoding="utf-8")
    r = root.as_posix()
    assert f'ServerRoot "{r}/bin/apache/httpd-2.4.68"' in conf
    assert "Listen 127.0.0.1:80" in conf and "Listen [::1]:80" in conf
    assert f'LoadModule fcgid_module "{r}/bin/apache/httpd-2.4.68/modules/mod_fcgid.so"' in conf
    assert "mod_rewrite.so" not in conf  # missing module is skipped, not a startup error
    assert f'FcgidWrapper "{r}/bin/php/php-8.3.33-Win32-vs16-x64/php-cgi.exe" .php' in conf
    assert f'PHPRC "{r}/etc/php/php-8.3.33-Win32-vs16-x64"' in conf
    assert "FcgidMaxRequestLen 268435456" in conf
    assert "CF-Connecting-IP" in conf  # phpMyAdmin is closed to tunnel visitors
    assert str(root) not in conf  # paths are written with forward slashes only
    # phpMyAdmin config + login helper are generated alongside
    assert (root / "bin/phpmyadmin/config.inc.php").is_file()
    assert (root / "bin/phpmyadmin/config.footer.inc.php").is_file()


@pytest.mark.parametrize(
    ("sid", "relative", "version", "present", "absent"),
    [
        ("mysql-8.4", "bin/mysql/mysql-8.4.3-winx64/bin/mysqld.exe", "8.4.3",
         ["mysql_native_password = ON", "port = 3308"], ["explicit_defaults_for_timestamp"]),
        ("mysql-5.6", "bin/mysql/mysql-5.6.12-win32/bin/mysqld.exe", "5.6.12",
         ["explicit_defaults_for_timestamp = 1", "port = 3306"], ["mysql_native_password"]),
        ("mariadb-11.8", "bin/mariadb/mariadb-11.8.9-winx64/bin/mariadbd.exe", "11.8.9",
         ["# MariaDB 11.8.9", "port = 3307"], ["mysql_native_password", "explicit_defaults"]),
    ],
)
def test_my_ini_per_version(root: Path, sid: str, relative: str, version: str,
                            present: list[str], absent: list[str]) -> None:
    e = engine(root)
    defn = next(d for d in e.services if d.id == sid)
    e.prepare(defn, inst(root, relative, version))
    ini = (root / f"etc/{sid}/my.ini").read_text(encoding="utf-8")
    assert f'datadir = "{root.as_posix()}/data/{sid}"' in ini
    assert f'socket = "varsha-{sid}"' in ini and "bind-address = 127.0.0.1" in ini
    for text in present:
        assert text in ini
    for text in absent:
        assert text not in ini


def test_phpmyadmin_servers(root: Path) -> None:
    e = engine(root, phpmyadmin_extra_servers=(ExtraDbServer("Other", "127.0.0.1", 3306),
                                               ExtraDbServer("Remote", "10.0.0.5", 3306),
                                               ExtraDbServer("Spare", "127.0.0.1", 3311)))
    labels = [(s.index, s.label) for s in e.db_servers()]
    assert labels == [
        (1, "MySQL 5.6.12 (32-bit) · port 3306"),
        (2, "MariaDB 11.8.9 · port 3307"),
        (3, "MySQL 8.4.3 · port 3308"),
        (4, "Remote · port 3306"),  # same port, different host: kept
        (5, "Spare · port 3311"),   # "Other" on 127.0.0.1:3306 duplicates MySQL 5.6: dropped
    ]
    assert e.phpmyadmin_server_index("mariadb-11.8") == 2
    e.generate_phpmyadmin()
    config = (root / "bin/phpmyadmin/config.inc.php").read_text(encoding="utf-8")
    assert "$cfg['Servers'][2]['port'] = '3307';" in config
    assert "$cfg['Servers'][1]['AllowNoPassword'] = true;" in config
    assert "$cfg['LoginCookieValidity'] = 28800;" in config
    footer = (root / "bin/phpmyadmin/config.footer.inc.php").read_text(encoding="utf-8")
    assert "'name' => 'MariaDB 11.8.9'" in footer and "fsockopen" in footer


def test_blowfish_secret_is_stable(root: Path) -> None:
    e = engine(root)
    e.generate_phpmyadmin()
    first = (root / "data/phpmyadmin/blowfish_secret.txt").read_text()
    e.generate_phpmyadmin()
    assert (root / "data/phpmyadmin/blowfish_secret.txt").read_text() == first
    assert len(first) == 32 and first.isalnum()
    assert f"$cfg['blowfish_secret'] = '{first}';" in (root / "bin/phpmyadmin/config.inc.php").read_text()


def test_write_if_changed_keeps_backup(tmp_path: Path) -> None:
    target = tmp_path / "x.conf"
    assert write_if_changed(target, "one\n")
    assert not write_if_changed(target, "one\n")  # unchanged: untouched
    assert write_if_changed(target, "two\n")
    assert target.read_text() == "two\n" and (tmp_path / "x.conf.bak").read_text() == "one\n"


def test_php_str_escaping() -> None:
    assert php_str("it's C:\\x") == "'it\\'s C:\\\\x'"
