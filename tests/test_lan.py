"""LAN sharing: the LAN vhost, port choice, firewall rule parsing and megha's checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from test_phase3_engine import make_engine, root  # noqa: F401  (fixture)
from varsha.config_engine.engine import CertPair
from varsha.errors import ConfigError
from varsha.helper import megha
from varsha.paths import AppPaths
from varsha.sites import lan


def fake_cert(engine, tmp: Path) -> None:  # type: ignore[no-untyped-def]
    engine.ensure_cert = lambda stem, names: CertPair((tmp / f"{stem}.pem").as_posix(), (tmp / f"{stem}-key.pem").as_posix())


def test_lan_vhost_serves_the_site_under_its_own_name(root: Path) -> None:  # noqa: F811
    engine = make_engine(root)
    fake_cert(engine, root)
    (root / "www/rdesk/public").mkdir(parents=True)
    assert engine.generate_lan({"rdesk.test": 8443}, "192.168.1.5", ["192.168.1.5", "PC"]) is True
    conf = (root / "etc/apache/lan-enabled/rdesk.test.conf").read_text(encoding="utf-8")
    assert conf.startswith("# varsha-generated LAN share")
    assert "Listen 0.0.0.0:8443 https" in conf and "<VirtualHost *:8443>" in conf
    assert f'DocumentRoot "{(root / "www/rdesk/public").as_posix()}"' in conf
    assert 'RequestHeader set Host "rdesk.test" early' in conf
    # links to the site (also JSON-escaped ones) and redirects move to the LAN address
    assert r'Substitute "s|https?:(\\\\)?/(\\\\)?/rdesk\.test(:[0-9]+)?|https:$1/$2/192.168.1.5:8443|i"' in conf
    assert 'Header always edit Location "^https?://rdesk\\.test(:[0-9]+)?" "https://192.168.1.5:8443"' in conf
    assert engine.generate_lan({"rdesk.test": 8443}, "192.168.1.5", ["192.168.1.5", "PC"]) is False


def test_lan_files_are_removed_when_sharing_stops(root: Path) -> None:  # noqa: F811
    engine = make_engine(root)
    fake_cert(engine, root)
    (root / "www/rdesk").mkdir(parents=True)
    (root / "www/shop").mkdir(parents=True)
    engine.generate_lan({"rdesk.test": 8443, "shop.test": 8444}, "10.0.0.7", ["10.0.0.7"])
    folder = root / "etc/apache/lan-enabled"
    hand_made = folder / "mine.conf"
    hand_made.write_text("# my own\n", encoding="utf-8")
    assert engine.generate_lan({"shop.test": 8444}, "10.0.0.7", ["10.0.0.7"]) is True
    assert sorted(p.name for p in folder.glob("*.conf")) == ["mine.conf", "shop.test.conf"]
    assert engine.generate_lan({}, None, []) is True
    assert sorted(p.name for p in folder.iterdir()) == ["mine.conf"]  # varsha's files only
    assert engine.generate_lan({}, None, []) is False


def test_unknown_site_is_skipped_and_https_is_required(root: Path) -> None:  # noqa: F811
    engine = make_engine(root)
    with pytest.raises(ConfigError, match="mkcert"):  # no mkcert in this root
        engine.generate_lan({"gone.test": 8443}, "10.0.0.7", ["10.0.0.7"])
    fake_cert(engine, root)
    assert engine.generate_lan({"gone.test": 8443}, "10.0.0.7", ["10.0.0.7"]) is False


def test_httpd_conf_includes_lan_folder(root: Path) -> None:  # noqa: F811
    text = (root / "etc/templates/httpd.conf.j2").read_text(encoding="utf-8")
    assert 'IncludeOptional "{{ etc }}/apache/lan-enabled/*.conf"' in text


def test_ports_are_remembered_and_never_shared(tmp_path: Path) -> None:
    shares = lan.LanShares(AppPaths(tmp_path))
    free = lambda port: True  # noqa: E731
    assert shares.pick_port("a.test", free) == lan.PORT_FIRST
    shares.add("a.test", lan.PORT_FIRST)
    assert shares.pick_port("b.test", free) == lan.PORT_FIRST + 1
    shares.add("b.test", lan.PORT_FIRST + 1)
    shares.clear()
    # next session: each site gets its old port back, so bookmarks keep working
    assert shares.pick_port("b.test", free) == lan.PORT_FIRST + 1
    assert shares.pick_port("c.test", free) == lan.PORT_FIRST + 2
    # a busy port is skipped
    assert shares.pick_port("a.test", lambda p: p != lan.PORT_FIRST) == lan.PORT_FIRST + 2
    with pytest.raises(ValueError, match="no free port"):
        shares.pick_port("a.test", lambda p: False)
    shares.add("a.test", lan.PORT_FIRST)
    shares.keep_only({"b.test"})
    assert shares.shares == {}


NETSH = """
Rule Name:                            varsha LAN share
----------------------------------------------------------------------
Description:                          varsha: sites shared on the LAN
Enabled:                              Yes
Direction:                            In
Profiles:                             Domain,Private
Grouping:
LocalIP:                              Any
RemoteIP:                             Any
Protocol:                             TCP
LocalPort:                            8443-8462
RemotePort:                           Any
Edge traversal:                       No
Program:                              {program}
InterfaceTypes:                       Any
Security:                             NotRequired
Rule source:                          Local Setting
Action:                               Allow
Ok.
"""


def test_firewall_rule_must_match_this_apache(tmp_path: Path) -> None:
    httpd = tmp_path / "bin/apache/httpd-2.4.68/bin/httpd.exe"
    rule = lan.parse_firewall_rule(NETSH.format(program=httpd))
    assert rule["LocalPort"] == "8443-8462" and rule["Enabled"] == "Yes"
    assert lan.firewall_rule_ok(rule, httpd)
    assert not lan.firewall_rule_ok(rule, tmp_path / "bin/apache/httpd-2.4.70/bin/httpd.exe")  # Apache upgraded
    assert not lan.firewall_rule_ok(None, httpd)
    assert not lan.firewall_rule_ok(dict(rule, Enabled="No"), httpd)


def test_megha_allows_only_varshas_own_httpd(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    httpd = bin_dir / "apache/httpd-2.4.68/bin/httpd.exe"
    httpd.parent.mkdir(parents=True)
    httpd.write_bytes(b"")
    assert megha.validate_program(str(httpd), bin_dir) == httpd.resolve()
    other = tmp_path / "evil" / "httpd.exe"
    other.parent.mkdir()
    other.write_bytes(b"")
    for bad in (str(other), str(bin_dir / "apache/httpd-2.4.68/bin/cmd.exe"), str(bin_dir / "../evil/httpd.exe"), ""):
        with pytest.raises(megha.HelperError):
            megha.validate_program(bad, bin_dir)


def test_lan_addresses_are_private_ipv4() -> None:
    for address in lan.lan_addresses():
        assert lan._usable(address.ip)
    assert lan._usable("192.168.1.5") and lan._usable("10.1.2.3") and lan._usable("172.20.0.4")
    assert not lan._usable("127.0.0.1") and not lan._usable("169.254.3.4") and not lan._usable("8.8.8.8")
