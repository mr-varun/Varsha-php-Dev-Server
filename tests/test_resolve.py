from __future__ import annotations

from pathlib import Path

import pytest

from varsha.errors import ConfigError
from varsha.paths import AppPaths
from varsha.php.versions import find_php_builds
from varsha.services.models import Installation, ServiceDef
from varsha.services.resolve import (
    build_context,
    detect_version,
    expand,
    find_installation,
    install_hint,
)


def touch(path: Path, content: bytes = b"") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("bin/apache/httpd-2.4.68-260617-Win64-VS18/bin/httpd.exe", "2.4.68"),
        ("bin/php/php-8.5.12-1-nts-Win32-vs17-x64/php.exe", "8.5.12"),
        ("bin/mysql/mysql-5.6.12-win32/bin/mysqld.exe", "5.6.12"),
        ("bin/mariadb/mariadb-11.8.9-winx64/bin/mariadbd.exe", "11.8.9"),
        ("bin/mailpit/mailpit.exe", None),
    ],
)
def test_detect_version(tmp_path: Path, relative: str, expected: str | None) -> None:
    assert detect_version(tmp_path / relative, tmp_path) == expected


def test_find_installation_picks_newest(tmp_path: Path) -> None:
    touch(tmp_path / "bin/apache/httpd-2.4.9/bin/httpd.exe")
    touch(tmp_path / "bin/apache/httpd-2.4.68/bin/httpd.exe")
    defn = ServiceDef(id="apache", name="Apache", type="apache", exe="bin/apache/*/bin/httpd.exe")
    inst = find_installation(tmp_path, defn)
    assert inst is not None and inst.version == "2.4.68"  # numeric, not string, comparison


def test_find_installation_missing(tmp_path: Path) -> None:
    defn = ServiceDef(id="x", name="X", type="mysql", exe="bin/mysql/mysql-8.4*/bin/mysqld.exe")
    assert find_installation(tmp_path, defn) is None


def test_install_hint() -> None:
    assert install_hint("bin/apache/*/bin/httpd.exe") == "bin\\apache\\<version folder>\\bin\\httpd.exe"
    assert install_hint("bin/mysql/mysql-8.4*/bin/mysqld.exe") == "bin\\mysql\\mysql-8.4...\\bin\\mysqld.exe"


def test_context_and_expand(tmp_path: Path) -> None:
    paths = AppPaths(tmp_path)
    exe = tmp_path / "bin/mysql/mysql-8.4.3-winx64/bin/mysqld.exe"
    defn = ServiceDef(id="mysql-8.4", name="MySQL", type="mysql", exe="x", ports=(3308, 33080),
                      config_file="etc/mysql-8.4/my.ini", data_dir="data/mysql-8.4")
    ctx = build_context(paths, defn, Installation(exe, "8.4.3"))
    out = expand(["--defaults-file={config}", "{home}", "{ports[1]}", "{port}", "{data_dir}"], ctx, defn.id)
    root = tmp_path.as_posix()
    assert out == [
        f"--defaults-file={root}/etc/mysql-8.4/my.ini",
        f"{root}/bin/mysql/mysql-8.4.3-winx64",
        "33080",
        "3308",
        f"{root}/data/mysql-8.4",
    ]
    assert all("\\" not in item for item in out)


def test_expand_bad_index(tmp_path: Path) -> None:
    defn = ServiceDef(id="s", name="S", type="t", exe="x", ports=(1,))
    ctx = build_context(AppPaths(tmp_path), defn, Installation(tmp_path / "a.exe", None))
    with pytest.raises(ConfigError):
        expand(["{ports[5]}"], ctx, "s")


def _fake_pe(machine: int) -> bytes:
    header = bytearray(512)
    header[0:2] = b"MZ"
    header[0x3C:0x40] = (0x80).to_bytes(4, "little")
    header[0x80:0x84] = b"PE\0\0"
    header[0x84:0x86] = machine.to_bytes(2, "little")
    return bytes(header)


def test_find_php_builds(tmp_path: Path) -> None:
    php = tmp_path / "bin/php"
    touch(php / "php-8.3.33-Win32-vs16-x64/php.exe", _fake_pe(0x8664))
    touch(php / "php-8.3.33-Win32-vs16-x64/php8ts.dll")
    touch(php / "php-8.3.33-Win32-vs16-x64/php-cgi.exe")
    touch(php / "php-5.4.33-nts-Win32-VC9-x86/php.exe", _fake_pe(0x014C))
    touch(php / "php-5.4.33-nts-Win32-VC9-x86/php5.dll")
    touch(php / "php-5.4.33-nts-Win32-VC9-x86/php_sockets.dll")  # must not count as "ts"
    touch(php / "no-version/php.exe")
    builds = find_php_builds(php, tmp_path)
    assert [b.label for b in builds] == ["8.3.33 TS x64", "5.4.33 NTS x86"]
    assert builds[0].cgi_exe is not None and builds[1].cgi_exe is None
