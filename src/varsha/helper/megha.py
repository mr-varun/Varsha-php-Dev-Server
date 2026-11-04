"""megha: the only part of varsha that runs as administrator.

    megha hosts-sync  --request <json> --result <json>
    megha dns-route-add --tld test     --result <json>
    megha dns-route-remove             --result <json>
    megha firewall-lan-add --program <varsha's httpd.exe> --result <json>
    megha firewall-lan-remove          --result <json>

Rules, because this runs elevated:
- Inputs are validated strictly: hostnames must end in the configured TLD, only loopback
  addresses are ever written, and the hosts file path is fixed (no option to point elsewhere).
- The hosts file is backed up (hosts.varsha.bak) before every write, and only varsha's
  marker block changes.
- The firewall rule has a fixed name and port range (varsha's LAN share ports), private and
  domain networks only, and the program must be an httpd.exe inside this varsha's bin folder.
- The result goes to a JSON file because the elevated process has no console to read from.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from varsha.errors import ConfigError
from varsha.paths import AppPaths
from varsha.sites import lan
from varsha.sites import hosts
from varsha.sites.discovery import validate_tld

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_NAMES = 5000
NRPT_COMMENT = "varsha"


class HelperError(Exception):
    pass


def validate_names(names: Any, tld: str) -> list[str]:
    if not isinstance(names, list) or len(names) > MAX_NAMES:
        raise HelperError("names must be a list of host names")
    suffix = "." + tld
    clean: list[str] = []
    for name in names:
        if not isinstance(name, str):
            raise HelperError("host names must be strings")
        name = name.lower()
        if not name.endswith(suffix) or not hosts.HOSTNAME_RE.match(name):
            raise HelperError(f"refusing host name {name!r}: it must end in {suffix}")
        clean.append(name)
    return clean


def hosts_sync(names: list[str], path: Path | None = None) -> str:
    """Rewrite varsha's block in the hosts file. `path` is for tests; the CLI never sets it."""
    path = path or hosts.hosts_path()
    text = hosts.read_text(path) if path.exists() else ""
    entries = hosts.entries_for(names, text)
    updated = hosts.apply_block(text, entries)
    if updated == text:
        return f"hosts file already up to date ({len(entries)} names)"
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".varsha.bak"))
    hosts.write_text(path, updated)
    flush_dns()
    return f"hosts file updated: {len(entries)} names"


def flush_dns() -> None:
    subprocess.run(["ipconfig", "/flushdns"], capture_output=True, creationflags=CREATE_NO_WINDOW)


def powershell(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                          capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=60)


def dns_route_remove() -> str:
    result = powershell(f"Get-DnsClientNrptRule | Where-Object Comment -eq '{NRPT_COMMENT}' | "
                        "Remove-DnsClientNrptRule -Force; Clear-DnsClientCache")
    if result.returncode != 0:
        raise HelperError(f"removing the DNS route failed: {result.stderr.strip()}")
    return "DNS route removed"


def dns_route_add(tld: str) -> str:
    dns_route_remove()  # never stack duplicate rules
    # The Name Resolution Policy Table sends only *.<tld> lookups to 127.0.0.1; every other
    # name keeps using the normal DNS servers, so nothing else on the PC is affected.
    result = powershell(f"Add-DnsClientNrptRule -Namespace '.{tld}' -NameServers '127.0.0.1' "
                        f"-Comment '{NRPT_COMMENT}'; Clear-DnsClientCache")
    if result.returncode != 0:
        raise HelperError(f"adding the DNS route failed: {result.stderr.strip()}")
    return f"*.{tld} now resolves through varsha's DNS (127.0.0.1)"


def validate_program(program: str, bin_dir: Path) -> Path:
    path = Path(program).resolve()
    try:
        path.relative_to(bin_dir.resolve())
    except ValueError:
        raise HelperError(f"refusing {program}: only varsha's own Apache can be allowed") from None
    if path.name.lower() != "httpd.exe" or not path.is_file():
        raise HelperError(f"refusing {program}: not an httpd.exe")
    return path


def netsh_firewall(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["netsh", "advfirewall", "firewall", *args], capture_output=True, text=True,
                          creationflags=CREATE_NO_WINDOW, timeout=60, errors="replace")


def firewall_lan_remove() -> str:
    netsh_firewall("delete", "rule", f"name={lan.FIREWALL_RULE}")  # exit 1 when there was none
    return "Firewall rule for LAN sharing removed"


def firewall_lan_add(program: Path) -> str:
    firewall_lan_remove()  # never stack duplicates (e.g. after Apache moved to a new version folder)
    result = netsh_firewall("add", "rule", f"name={lan.FIREWALL_RULE}", "dir=in", "action=allow",
                            f"program={program}", "protocol=TCP", f"localport={lan.PORT_RANGE}",
                            "profile=private,domain", "enable=yes",
                            "description=varsha: sites shared on the LAN from the control panel")
    if result.returncode != 0:
        raise HelperError(f"adding the firewall rule failed: {(result.stdout or result.stderr).strip()}")
    return f"Firewall allows ports {lan.PORT_RANGE} on private networks"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="megha")
    parser.add_argument("command", choices=["hosts-sync", "dns-route-add", "dns-route-remove",
                                            "firewall-lan-add", "firewall-lan-remove"])
    parser.add_argument("--request")
    parser.add_argument("--tld", default="test")
    parser.add_argument("--program")
    parser.add_argument("--result", required=True)
    args = parser.parse_args(argv)
    outcome: dict[str, Any]
    try:
        tld = validate_tld(args.tld)
        if args.command == "hosts-sync":
            if not args.request:
                raise HelperError("--request is required")
            request = json.loads(Path(args.request).read_text(encoding="utf-8"))
            tld = validate_tld(str(request.get("tld", tld)))
            message = hosts_sync(validate_names(request.get("names"), tld))
        elif args.command == "dns-route-add":
            message = dns_route_add(tld)
        elif args.command == "firewall-lan-add":
            message = firewall_lan_add(validate_program(args.program or "", AppPaths.discover().bin))
        elif args.command == "firewall-lan-remove":
            message = firewall_lan_remove()
        else:
            message = dns_route_remove()
        outcome = {"ok": True, "message": message}
    except (HelperError, ConfigError, OSError, ValueError, subprocess.SubprocessError) as exc:
        outcome = {"ok": False, "message": str(exc)}
    Path(args.result).write_text(json.dumps(outcome), encoding="utf-8")
    return 0 if outcome["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
