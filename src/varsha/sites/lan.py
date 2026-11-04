"""Share a site with colleagues on the same network: https://<this PC's LAN IP>:<port>/.

Apache normally listens on loopback only. Sharing a site adds one extra vhost on its own port
(etc/apache/lan-enabled/<domain>.conf, from lan.conf.j2) that serves the same folder: the site
still sees its own host name over HTTPS, and absolute links back to it are rewritten to the LAN
address. Nothing in the project changes.

- Shares are runtime state: none at startup (leftover files from a crash are removed) and all of
  them end when varsha quits, even when services keep running. Only the port each site got is
  remembered (data/lan-ports.json), so a colleague's bookmark keeps working next time.
- Windows Firewall needs one inbound rule (megha, one UAC prompt) for httpd.exe on the LAN port
  range, private and domain networks only. It stays: with nothing shared nothing listens there.
- The certificate is mkcert's, for the LAN IPs and the computer name. Other laptops don't trust
  the mkcert CA, so colleagues accept a browser warning once.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from varsha.paths import AppPaths
from varsha.services.process import CREATE_NO_WINDOW

logger = logging.getLogger(__name__)

PORT_FIRST = 8443
PORT_COUNT = 20
PORT_RANGE = f"{PORT_FIRST}-{PORT_FIRST + PORT_COUNT - 1}"
FIREWALL_RULE = "varsha LAN share"
CACHE_SECONDS = 30.0
# Adapters that are never "the Wi-Fi": Hyper-V/WSL/Docker switches, VM host-only networks, VPN taps.
VIRTUAL_HINTS = ("vethernet", "virtualbox", "vmware", "hyper-v", "wsl", "docker", "loopback",
                 "bluetooth", "tap-", "tailscale", "zerotier", "npcap")
NETWORK_CATEGORY = {0: "Public", 1: "Private", 2: "Domain"}


@dataclass(frozen=True)
class LanAddress:
    ip: str
    interface: str


def _usable(ip: str) -> bool:
    try:
        address = ipaddress.IPv4Address(ip)
    except ValueError:
        return False
    return address.is_private and not (address.is_loopback or address.is_link_local)


def lan_addresses() -> list[LanAddress]:
    """This PC's private IPv4 addresses on adapters that are up, the default-route one first."""
    try:
        stats = psutil.net_if_stats()
        found = [LanAddress(a.address, name)
                 for name, addrs in psutil.net_if_addrs().items()
                 if stats.get(name) is not None and stats[name].isup
                 and not any(hint in name.lower() for hint in VIRTUAL_HINTS)
                 for a in addrs if a.family == socket.AF_INET and _usable(a.address)]
    except (OSError, RuntimeError):
        found = []
    primary = _default_route_ip()
    found.sort(key=lambda a: a.ip != primary)
    return found


def _default_route_ip() -> str | None:
    """The address Windows would use to reach the network. UDP connect() sends nothing."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return str(sock.getsockname()[0])
    except OSError:
        return None
    finally:
        sock.close()


def _run(cmd: list[str], timeout: float = 20) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=CREATE_NO_WINDOW, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None


def network_categories() -> dict[str, str]:
    """{adapter name: Public | Private | Domain} from Windows (no admin rights needed)."""
    result = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                   "Get-NetConnectionProfile | Select-Object InterfaceAlias,NetworkCategory | ConvertTo-Json -Compress"])
    if result is None or result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return {}
    rows = data if isinstance(data, list) else [data]
    out: dict[str, str] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("InterfaceAlias"):
            category = row.get("NetworkCategory")
            out[str(row["InterfaceAlias"])] = NETWORK_CATEGORY.get(category, str(category)) if isinstance(category, int) else str(category)
    return out


def parse_firewall_rule(text: str) -> dict[str, str]:
    """The first rule block of `netsh advfirewall firewall show rule ... verbose` as {field: value}."""
    fields: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line or line.startswith("-"):
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key == "Rule Name" and fields:
            break
        fields.setdefault(key, value.strip())
    return fields


def firewall_rule() -> dict[str, str] | None:
    result = _run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={FIREWALL_RULE}", "verbose"])
    if result is None or result.returncode != 0:
        return None
    fields = parse_firewall_rule(result.stdout)
    return fields or None


def firewall_rule_ok(rule: dict[str, str] | None, program: Path | None) -> bool:
    if not rule or program is None:
        return False
    return (rule.get("Enabled", "").lower() == "yes" and rule.get("LocalPort", "") == PORT_RANGE
            and Path(rule.get("Program", "")).resolve() == program.resolve())


# phpMyAdmin bundles bacon-qr-code (for its two-factor login); varsha borrows it, so the QR comes
# from a proven encoder and nothing is downloaded. It prints the module matrix, the panel draws it.
QR_PHP = (
    "require $argv[1] . '/vendor/autoload.php';"
    "$qr = BaconQrCode\\Encoder\\Encoder::encode($argv[2], BaconQrCode\\Common\\ErrorCorrectionLevel::M(), 'UTF-8');"
    "$m = $qr->getMatrix(); $rows = [];"
    "for ($y = 0; $y < $m->getHeight(); $y++) { $r = '';"
    " for ($x = 0; $x < $m->getWidth(); $x++) { $r .= $m->get($x, $y) ? '1' : '0'; } $rows[] = $r; }"
    "echo json_encode($rows);"
)


def qr_rows(php_exe: Path, ini_dir: Path | None, phpmyadmin: Path, text: str) -> list[str]:
    """The QR code of `text` as rows of "0"/"1" (dark = 1), without the quiet zone."""
    if not (phpmyadmin / "vendor" / "bacon" / "bacon-qr-code").is_dir():
        raise ValueError("QR codes need phpMyAdmin in bin\\phpmyadmin (its QR library is used)")
    ini = ["-c", str(ini_dir)] if ini_dir is not None and (ini_dir / "php.ini").is_file() else ["-n"]
    result = _run([str(php_exe), *ini, "-r", QR_PHP, "--", str(phpmyadmin), text], timeout=30)
    if result is None or result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[:300] if result else "PHP didn't start"
        raise ValueError(f"Making the QR code failed: {detail}")
    rows = json.loads(result.stdout)
    if not (isinstance(rows, list) and rows and all(isinstance(r, str) and set(r) <= {"0", "1"} for r in rows)):
        raise ValueError("Making the QR code failed: unexpected output")
    return rows


class LanShares:
    """Which sites are shared on the LAN, on which ports. The SiteManager renders and reloads."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self._shares: dict[str, int] = {}
        self._lock = threading.Lock()
        self._ports_file = paths.data / "lan-ports.json"
        self._cache: dict[str, tuple[float, Any]] = {}
        self._refreshing: set[str] = set()
        self.rendered_address: str | None = None

    # ------------------------------------------------------------------ shares

    @property
    def shares(self) -> dict[str, int]:
        with self._lock:
            return dict(self._shares)

    def _remembered(self) -> dict[str, int]:
        try:
            data = json.loads(self._ports_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): int(v) for k, v in data.items()
                if isinstance(v, int) and PORT_FIRST <= v < PORT_FIRST + PORT_COUNT} if isinstance(data, dict) else {}

    def _remember(self, domain: str, port: int) -> None:
        ports = self._remembered()
        ports = {d: p for d, p in ports.items() if p != port}  # a port belongs to one site
        ports[domain] = port
        self._ports_file.parent.mkdir(parents=True, exist_ok=True)
        self._ports_file.write_text(json.dumps(ports, indent=2, sort_keys=True), encoding="utf-8")

    def pick_port(self, domain: str, is_free: Callable[[int], bool]) -> int:
        """The port this site had before if it's still free, else the first free one in the range."""
        with self._lock:
            taken = set(self._shares.values())
        remembered = self._remembered()
        others = {p for d, p in remembered.items() if d != domain}
        wanted = remembered.get(domain)
        candidates = ([wanted] if wanted else []) + [p for p in range(PORT_FIRST, PORT_FIRST + PORT_COUNT) if p not in others]
        candidates += [p for p in others]  # last resort: a port another (unshared) site used before
        for port in dict.fromkeys(candidates):
            if port not in taken and is_free(port):
                return port
        raise ValueError(f"no free port left in {PORT_RANGE} for LAN sharing")

    def add(self, domain: str, port: int) -> None:
        with self._lock:
            self._shares[domain] = port
        self._remember(domain, port)

    def remove(self, domain: str) -> bool:
        with self._lock:
            return self._shares.pop(domain, None) is not None

    def clear(self) -> bool:
        with self._lock:
            had = bool(self._shares)
            self._shares.clear()
            return had

    def keep_only(self, domains: set[str]) -> None:
        """Drop shares of sites whose folder is gone."""
        with self._lock:
            for domain in [d for d in self._shares if d not in domains]:
                del self._shares[domain]

    # ------------------------------------------------------------------ the network

    def _cached(self, key: str, fn: Callable[[], Any], refresh: bool) -> Any:
        """netsh / PowerShell take up to a second, too slow for every /api/state: a stale value
        is returned at once and refreshed in the background. `refresh` (after an action) waits."""
        now = time.monotonic()
        hit = self._cache.get(key)
        if hit and not refresh:
            if now - hit[0] >= CACHE_SECONDS and key not in self._refreshing:
                self._refreshing.add(key)
                self._cache[key] = (now, hit[1])  # one refresh at a time

                def update() -> None:
                    try:
                        self._cache[key] = (time.monotonic(), fn())
                    finally:
                        self._refreshing.discard(key)

                threading.Thread(target=update, name=f"lan-{key}", daemon=True).start()
            return hit[1]
        value = fn()
        self._cache[key] = (now, value)
        return value

    def addresses(self) -> list[LanAddress]:
        return lan_addresses()

    def address(self) -> str | None:
        found = self.addresses()
        return found[0].ip if found else None

    def cert_names(self) -> list[str]:
        names = [a.ip for a in self.addresses()]
        host = socket.gethostname()
        return names + [host] if host else names

    def firewall(self, program: Path | None, refresh: bool = False) -> bool:
        return firewall_rule_ok(self._cached("firewall", firewall_rule, refresh), program)

    def categories(self, refresh: bool = False) -> dict[str, str]:
        return self._cached("categories", network_categories, refresh)

    def forget_cache(self) -> None:
        self._cache.clear()

    def warm(self) -> None:
        """Fill the cache in the background at startup, so the panel's first load doesn't wait."""
        def fill() -> None:
            for key, fn in (("firewall", firewall_rule), ("categories", network_categories)):
                try:
                    self._cache[key] = (time.monotonic(), fn())
                except Exception:
                    logger.exception("Reading %s failed", key)

        threading.Thread(target=fill, name="lan-warm", daemon=True).start()

    def status(self, program: Path | None, refresh: bool = False) -> dict[str, Any]:
        found = self.addresses()
        primary = found[0] if found else None
        category = self.categories(refresh).get(primary.interface, "") if primary else ""
        shares = self.shares
        return {
            "address": primary.ip if primary else None,
            "interface": primary.interface if primary else None,
            "others": [a.ip for a in found[1:]],
            "category": category,  # "Public" networks block the firewall rule
            "firewall": self.firewall(program, refresh),
            "port_range": PORT_RANGE,
            "shares": [{"domain": d, "port": p,
                        "url": f"https://{primary.ip}:{p}/" if primary else None}
                       for d, p in sorted(shares.items())],
        }
