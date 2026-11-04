from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from dnslib import QTYPE, RCODE, DNSRecord

from conftest import REPO_ROOT
from varsha.config_engine.engine import ConfigEngine, restrict_to_current_user
from varsha.dns.server import TldResolver
from varsha.paths import AppPaths
from varsha.secrets import CredentialStore
from varsha.services.config import parse_services
from varsha.services.manager import default_locator
from varsha.services.remote import RemoteDbStore
from varsha.settings import Settings

APACHE = {"id": "apache", "name": "Apache", "type": "apache", "exe": "bin/apache/*/bin/httpd.exe",
          "args": ["-f", "{config}"], "ports": [80, 443], "config_template": "httpd.conf.j2",
          "config_file": "etc/apache/httpd.conf",
          "reload": {"method": "apache_signal", "test_command": ["{exe}", "-t"]}}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copytree(REPO_ROOT / "etc" / "templates", tmp_path / "etc" / "templates")
    AppPaths(tmp_path).ensure_dirs()
    (tmp_path / "bin/apache/httpd-2.4.68/bin").mkdir(parents=True)
    (tmp_path / "bin/apache/httpd-2.4.68/bin/httpd.exe").write_bytes(b"")
    (tmp_path / "bin/phpmyadmin").mkdir(parents=True)
    (tmp_path / "bin/phpmyadmin/index.php").write_text("<?php")
    return tmp_path


def make_engine(root: Path, remote: RemoteDbStore | None = None) -> ConfigEngine:
    return ConfigEngine(AppPaths(root), Settings(), parse_services({"services": [APACHE]}), remote=remote)


# ---------------------------------------------------------------------- vhosts

def test_vhosts_follow_www(root: Path) -> None:
    engine = make_engine(root)
    apache = engine.services[0]
    (root / "www/shop/public").mkdir(parents=True)
    hand_made = root / "etc/apache/sites-enabled/custom.conf"
    hand_made.parent.mkdir(parents=True, exist_ok=True)
    hand_made.write_text("# my own vhost\n", encoding="utf-8")

    assert engine.generate_sites(apache) is True
    conf = (root / "etc/apache/sites-enabled/shop.test.conf").read_text(encoding="utf-8")
    assert conf.startswith("# varsha-generated")
    assert "ServerName shop.test" in conf and "ServerAlias *.shop.test" in conf
    assert f'DocumentRoot "{(root / "www/shop/public").as_posix()}"' in conf
    assert "SSLEngine" not in conf  # no mkcert in this tmp root: HTTP only
    assert engine.generate_sites(apache) is False  # nothing changed

    shutil.rmtree(root / "www/shop")
    assert engine.generate_sites(apache) is True
    assert not (root / "etc/apache/sites-enabled/shop.test.conf").exists()
    assert hand_made.exists()  # files varsha didn't write are never deleted


def test_httpd_conf_default_site_and_https_listener(root: Path) -> None:
    engine = make_engine(root)
    from varsha.services.models import Installation
    engine.prepare(engine.services[0], Installation(root / "bin/apache/httpd-2.4.68/bin/httpd.exe", "2.4.68"))
    conf = (root / "etc/apache/httpd.conf").read_text(encoding="utf-8")
    assert "Listen 127.0.0.1:443" in conf
    assert conf.index("ServerName localhost\n") < conf.index("IncludeOptional")  # default vhost comes first
    assert "<IfModule ssl_module>" in conf


def test_every_include_folder_exists_on_a_fresh_install(root: Path) -> None:
    """IncludeOptional forgives a pattern with no matches, but a missing folder stops Apache
    ("Could not open directory ...lan-enabled"), which happened on the first start after an update."""
    import re

    from varsha.services.models import Installation
    engine = make_engine(root)
    engine.prepare(engine.services[0], Installation(root / "bin/apache/httpd-2.4.68/bin/httpd.exe", "2.4.68"))
    conf = (root / "etc/apache/httpd.conf").read_text(encoding="utf-8")
    folders = [Path(p).parent for p in re.findall(r'^\s*Include(?:Optional)?\s+"([^"]+)"', conf, re.MULTILINE)]
    assert len(folders) >= 2
    for folder in folders:
        assert folder.is_dir(), folder


# ---------------------------------------------------------------------- remote databases in phpMyAdmin

def test_remote_server_auto_login_config(root: Path) -> None:
    creds = CredentialStore(root / "data/credentials.json")
    store = RemoteDbStore(root / "config/remote-databases.json", creds)
    auto = store.save({"name": "Prod RDS", "host": "db.abc.ap-south-1.rds.amazonaws.com", "user": "admin",
                       "password": "pa'ss", "remember": True, "ssl": True})
    manual = store.save({"name": "Staging", "host": "10.0.0.5", "port": 3307, "user": "dev", "remember": True})
    engine = make_engine(root, remote=store)
    engine.generate_phpmyadmin()
    config = (root / "bin/phpmyadmin/config.inc.php").read_text(encoding="utf-8")
    first = engine.phpmyadmin_remote_index(auto.id)
    second = engine.phpmyadmin_remote_index(manual.id)
    assert f"$cfg['Servers'][{first}]['auth_type'] = 'config';" in config
    assert f"$cfg['Servers'][{first}]['ssl'] = true;" in config
    assert f"$cfg['Servers'][{second}]['auth_type'] = 'cookie';" in config  # no saved password
    assert "pa'ss" not in config and "pa\\'ss" not in config  # the password is never in config.inc.php
    secrets = (root / "data/phpmyadmin/remote-secrets.php").read_text(encoding="utf-8")
    assert "'pa\\'ss'" in secrets
    assert "pa'ss" not in (root / "config/remote-databases.json").read_text(encoding="utf-8")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACLs")
def test_secrets_file_is_private(tmp_path: Path) -> None:
    probe = tmp_path / "x.php"
    probe.write_text("x")
    restrict_to_current_user(probe)
    acl = subprocess.run(["icacls", str(probe)], capture_output=True, text=True).stdout
    assert "BUILTIN\\Users" not in acl and "Authenticated Users" not in acl and "(I)" not in acl


# ---------------------------------------------------------------------- DNS + built-ins

def test_dns_resolver_answers_test_names() -> None:
    resolver = TldResolver("test", upstreams=["127.0.0.1"])  # loopback upstreams are dropped
    assert resolver.upstreams == []
    a = resolver.resolve(DNSRecord.question("api.shop.test", "A"), None)
    assert [str(r.rdata) for r in a.rr] == ["127.0.0.1"]
    aaaa = resolver.resolve(DNSRecord.question("shop.test", "AAAA"), None)
    assert [str(r.rdata) for r in aaaa.rr] == ["::1"]
    other = resolver.resolve(DNSRecord.question("example.com", "A"), None)
    assert other.header.rcode == RCODE.SERVFAIL  # no upstream configured in this test
    assert not resolver.is_local("test") and not resolver.is_local("nottest")
    assert QTYPE.A == 1


def test_builtin_service_runs_as_module() -> None:
    (dns,) = parse_services({"services": [{"id": "dns", "name": "DNS", "type": "builtin",
                                            "module": "varsha.dns.server", "ports": [53]}]})
    inst = default_locator(Path("."), dns)
    assert inst is not None and Path(inst.exe) == Path(sys.executable)


def test_module_must_be_varsha_and_builtin() -> None:
    from varsha.errors import ConfigError
    with pytest.raises(ConfigError, match="varsha module"):
        parse_services({"services": [{"id": "x", "name": "X", "type": "builtin", "module": "os"}]})
    with pytest.raises(ConfigError, match="only for type"):
        parse_services({"services": [{"id": "x", "name": "X", "type": "generic", "exe": "bin/x.exe", "module": "varsha.x"}]})
