"""Parse and normalise what the user wants to share ("localhost:8025", "myapp.test", ...)."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from varsha.errors import ConfigError

LOCAL_NAMES = frozenset({"localhost"})
LOCAL_TLDS = (".test", ".localhost")


@dataclass(frozen=True)
class TunnelTarget:
    scheme: str
    host: str  # as typed, e.g. "myapp.test"
    port: int
    path: str  # only used to build the link to open; the whole host is shared

    @property
    def origin(self) -> str:
        """What cloudflared connects to.

        Always 127.0.0.1, never "localhost": cloudflared may resolve localhost to ::1 first,
        and Apache/Mailpit/MySQL here listen on 127.0.0.1 only. *.test names are routed by the
        Host header instead, so they work even before the hosts file has an entry.
        """
        return f"{self.scheme}://127.0.0.1:{self.port}"

    @property
    def host_header(self) -> str:
        """The Host header the local server sees, so Apache picks the right vhost."""
        default = 443 if self.scheme == "https" else 80
        return self.host if self.port == default else f"{self.host}:{self.port}"

    @property
    def display(self) -> str:
        return f"{self.scheme}://{self.host_header}{self.path if self.path != '/' else ''}"

    @property
    def shares_localhost(self) -> bool:
        """True when the default server is shared, which includes /phpmyadmin from Phase 2 on."""
        return not self.host.endswith(LOCAL_TLDS)


def _is_loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_target(text: str) -> TunnelTarget:
    raw = text.strip()
    if not raw:
        raise ConfigError("Type a local address to share, for example http://localhost:8025 or myapp.test")
    if "://" not in raw:
        raw = "http://" + raw
    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ConfigError(f"Only http:// and https:// addresses can be shared (got {parts.scheme}://)")
    host = (parts.hostname or "").lower()
    if not host:
        raise ConfigError(f'"{text.strip()}" has no host name')
    if not (host in LOCAL_NAMES or host.endswith(LOCAL_TLDS) or _is_loopback(host)):
        raise ConfigError(
            f'"{host}" is not a local address. Only localhost, 127.0.0.1 and local sites '
            "(*.test) can be shared."
        )
    try:
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise ConfigError(f"Invalid port in {text.strip()!r}") from exc
    if _is_loopback(host):
        host = "localhost"  # the vhost name a local server expects for a bare loopback address
    return TunnelTarget(scheme=scheme, host=host, port=port, path=parts.path or "/")
