from __future__ import annotations

import json
from pathlib import Path

import pytest

from varsha.errors import ConfigError
from varsha.helper import megha
from varsha.sites import hosts
from varsha.sites.discovery import SiteOverride, discover_sites, load_overrides, slugify, validate_tld

LARAGON_HOSTS = (
    "# Copyright (c) 1993-2009 Microsoft Corp.\r\n"
    "127.0.0.1       localhost\r\n"
    "127.0.0.1      banking.test         #laragon magic!   \r\n"
)


# ---------------------------------------------------------------------- discovery

@pytest.mark.parametrize(("name", "slug"), [("shop", "shop"), ("My_Blog", "my-blog"), ("Client Site 2", "client-site-2"),
                                            ("--x--", "x"), ("!!!", None)])
def test_slugify(name: str, slug: str | None) -> None:
    assert slugify(name) == slug


@pytest.mark.parametrize("tld", ["dev", "app", "com", "te st", ""])
def test_rejects_bad_tlds(tld: str) -> None:
    with pytest.raises(ConfigError):
        validate_tld(tld)


def test_discover_sites(tmp_path: Path) -> None:
    (tmp_path / "shop" / "public").mkdir(parents=True)
    (tmp_path / "My_Blog").mkdir()
    (tmp_path / "my-blog").mkdir()        # same host name as My_Blog: skipped
    (tmp_path / ".git").mkdir()           # hidden: skipped
    (tmp_path / "legacy" / "web").mkdir(parents=True)
    (tmp_path / "off").mkdir()
    (tmp_path / "notes.txt").write_text("not a folder")
    sites = discover_sites(tmp_path, "test", {"legacy": SiteOverride(docroot="web"), "off": SiteOverride(enabled=False)})
    by_domain = {s.domain: s for s in sites}
    assert list(by_domain) == ["legacy.test", "my-blog.test", "shop.test"]
    assert by_domain["shop.test"].docroot == tmp_path / "shop" / "public"
    assert by_domain["legacy.test"].docroot == tmp_path / "legacy" / "web"
    assert by_domain["shop.test"].aliases == ("*.shop.test",)
    assert by_domain["shop.test"].hosts_names == ("shop.test", "www.shop.test")


def test_overrides_file(tmp_path: Path) -> None:
    path = tmp_path / "sites.json"
    path.write_text(json.dumps({"sites": {"Shop": {"docroot": "dist"}, "_comment": "x"}}), encoding="utf-8")
    assert load_overrides(path) == {"shop": SiteOverride(docroot="dist")}
    path.write_text(json.dumps({"sites": {"shop": {"docroot": "../../Windows"}}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="inside the site folder"):
        load_overrides(path)


# ---------------------------------------------------------------------- hosts file block

def test_block_added_and_outside_untouched() -> None:
    text = hosts.apply_block(LARAGON_HOSTS, ["shop.test", "www.shop.test"])
    assert text.startswith(LARAGON_HOSTS)  # Laragon's lines byte for byte
    assert "\r\n127.0.0.1\tshop.test\r\n::1\tshop.test\r\n" in text
    assert hosts.current_names(text) == ["shop.test", "www.shop.test"]
    assert hosts.apply_block(text, ["shop.test", "www.shop.test"]) == text  # idempotent


def test_block_replaced_and_removed() -> None:
    text = hosts.apply_block(LARAGON_HOSTS + "127.0.0.1 after.example\r\n", ["a.test"])
    text = hosts.apply_block(text, ["b.test"])
    assert hosts.current_names(text) == ["b.test"] and text.count(hosts.START) == 1
    cleared = hosts.apply_block(text, [])
    assert hosts.START not in cleared and "banking.test" in cleared


def test_names_mapped_elsewhere_are_skipped() -> None:
    assert hosts.entries_for(["banking.test", "shop.test", "BAD NAME", "shop.test"], LARAGON_HOSTS) == ["shop.test"]


def test_in_sync() -> None:
    text = hosts.apply_block(LARAGON_HOSTS, hosts.entries_for(["banking.test", "shop.test"], LARAGON_HOSTS))
    assert hosts.is_in_sync(text, ["banking.test", "shop.test"])
    assert not hosts.is_in_sync(text, ["new.test"])


def test_latin1_roundtrip_keeps_odd_bytes(tmp_path: Path) -> None:
    path = tmp_path / "hosts"
    raw = "# café – entry\n127.0.0.1 x\n".encode("cp1252")
    path.write_bytes(raw)
    hosts.write_text(path, hosts.read_text(path))
    assert path.read_bytes() == raw


# ---------------------------------------------------------------------- megha (elevated helper)

def test_megha_validates_names() -> None:
    assert megha.validate_names(["Shop.test"], "test") == ["shop.test"]
    for bad in (["evil.com"], ["test"], ["a.test\n127.0.0.1 bank.com"], [5], "shop.test"):
        with pytest.raises(megha.HelperError):
            megha.validate_names(bad, "test")


def test_megha_hosts_sync_backs_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(megha, "flush_dns", lambda: None)
    path = tmp_path / "hosts"
    path.write_bytes(LARAGON_HOSTS.encode("latin-1"))
    message = megha.hosts_sync(["shop.test", "banking.test"], path=path)
    assert "1 names" in message
    assert (tmp_path / "hosts.varsha.bak").read_bytes() == LARAGON_HOSTS.encode("latin-1")
    assert "already up to date" in megha.hosts_sync(["shop.test", "banking.test"], path=path)


def test_megha_cli_writes_result_and_refuses_foreign_names(tmp_path: Path) -> None:
    request = tmp_path / "req.json"
    result = tmp_path / "res.json"
    request.write_text(json.dumps({"tld": "test", "names": ["evil.com"]}), encoding="utf-8")
    assert megha.main(["hosts-sync", "--request", str(request), "--result", str(result)]) == 1
    outcome = json.loads(result.read_text(encoding="utf-8"))
    assert outcome["ok"] is False and "evil.com" in outcome["message"]
