"""Remote MySQL / MariaDB servers (AWS RDS, Aurora, a VPS...) that phpMyAdmin should manage.

Stored in config/remote-databases.json without passwords. Passwords live DPAPI-encrypted in
the CredentialStore under "remote:<id>". "remember" means phpMyAdmin logs in by itself; for
that the password has to be readable by PHP, so the config engine writes it to a single file
that only the current Windows user can open (see ConfigEngine.generate_phpmyadmin).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import subprocess
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from varsha.errors import ConfigError, ServiceError
from varsha.secrets import CredentialStore, option_value
from varsha.services import process as proc_utils

logger = logging.getLogger(__name__)

HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
ID_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class RemoteDb:
    id: str
    name: str
    host: str
    port: int = 3306
    user: str = "admin"
    ssl: bool = True  # RDS / Aurora usually require TLS (require_secure_transport)
    ssl_verify: bool = False  # verify the server certificate against ssl_ca
    ssl_ca: str | None = None  # e.g. AWS's global-bundle.pem
    remember: bool = True  # phpMyAdmin logs in automatically with the saved password

    @property
    def credential_key(self) -> str:
        return f"remote:{self.id}"


def _clean_text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} is required")
    value = value.strip()
    if len(value) > limit or any(ord(ch) < 32 for ch in value):
        raise ConfigError(f"{field} must be at most {limit} characters, on one line")
    return value


def validate(data: dict[str, Any], existing_id: str | None = None) -> RemoteDb:
    name = _clean_text(data.get("name"), "Name", 60)
    host = _clean_text(data.get("host"), "Host", 253)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not HOST_RE.match(host):
            raise ConfigError(f'"{host}" is not a valid host name, e.g. mydb.abc123.ap-south-1.rds.amazonaws.com') from None
    port = data.get("port", 3306)
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ConfigError("Port must be a number") from None
    if not 1 <= port <= 65535:
        raise ConfigError("Port must be between 1 and 65535")
    user = _clean_text(data.get("user"), "Username", 80)
    ssl_ca = data.get("ssl_ca") or None
    if ssl_ca is not None:
        ssl_ca = _clean_text(ssl_ca, "CA file", 400)
        if not Path(ssl_ca).is_file():
            raise ConfigError(f"CA file not found: {ssl_ca}")
    ssl = bool(data.get("ssl", True))
    ssl_verify = bool(data.get("ssl_verify", False)) and ssl
    if ssl_verify and not ssl_ca:
        raise ConfigError("Verifying the certificate needs a CA file (for RDS: global-bundle.pem from AWS)")
    return RemoteDb(id=existing_id or "", name=name, host=host, port=port, user=user, ssl=ssl,
                    ssl_verify=ssl_verify, ssl_ca=ssl_ca, remember=bool(data.get("remember", True)))


class RemoteDbStore:
    def __init__(self, path: Path, credentials: CredentialStore) -> None:
        self.path = path
        self.credentials = credentials

    def list(self) -> list[RemoteDb]:
        if not self.path.is_file():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ConfigError(f"{self.path.name} is not valid JSON: {exc}") from exc
        items = raw.get("servers", []) if isinstance(raw, dict) else []
        servers: list[RemoteDb] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            try:
                servers.append(validate(item, existing_id=str(item["id"])))
            except ConfigError as exc:
                logger.warning("Skipping remote database %r: %s", item.get("name"), exc)
        return servers

    def get(self, db_id: str) -> RemoteDb:
        for server in self.list():
            if server.id == db_id:
                return server
        raise ServiceError(f"No remote database {db_id!r}")

    def password(self, server: RemoteDb) -> str:
        return self.credentials.get(server.credential_key)

    def has_password(self, server: RemoteDb) -> bool:
        return bool(self.password(server))

    def save(self, data: dict[str, Any], db_id: str | None = None) -> RemoteDb:
        """Add (db_id None) or update a server. A non-empty "password" replaces the saved one;
        an empty one keeps it, unless "forget_password" is set."""
        servers = self.list()
        if db_id is not None and not any(s.id == db_id for s in servers):
            raise ServiceError(f"No remote database {db_id!r}")
        server = validate(data, existing_id=db_id)
        if db_id is None:
            base = ID_RE.sub("-", server.name.lower()).strip("-")[:40] or "remote"
            taken = {s.id for s in servers}
            new_id, n = base, 2
            while new_id in taken:
                new_id, n = f"{base}-{n}", n + 1
            server = replace(server, id=new_id)
        servers = [s for s in servers if s.id != server.id] + [server]
        self._write(servers)
        password = data.get("password")
        if isinstance(password, str) and password:
            self.credentials.set(server.credential_key, password)
        elif data.get("forget_password"):
            self.credentials.set(server.credential_key, "")
        return server

    def delete(self, db_id: str) -> None:
        server = self.get(db_id)
        self._write([s for s in self.list() if s.id != db_id])
        self.credentials.set(server.credential_key, "")

    def _write(self, servers: list[RemoteDb]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_comment": "Remote MySQL/MariaDB servers for phpMyAdmin. Passwords are NOT stored here "
                        "(they are DPAPI-encrypted in data/credentials.json).",
            "servers": [asdict(s) for s in sorted(servers, key=lambda s: s.name.lower())],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


_HINTS = (
    ("2005", " The host name doesn't exist in DNS: copy the endpoint again from the RDS console "
             "(Connectivity & security). A deleted or renamed instance loses its endpoint."),
    ("2003", " Nothing answered on that port. Check that the instance is running, \"Publicly accessible\" "
             "is Yes, and its security group allows your IP address on this port."),
    ("1045", " Wrong username or password."),
    ("3159", " The server requires SSL: tick \"Use SSL\"."),
    ("2026", " SSL failed: check the CA file, or untick \"Verify the server certificate\"."),
)


def _hint(detail: str) -> str:
    return next((hint for code, hint in _HINTS if f"ERROR {code}" in detail), "")


def test_connection(client: Path, server: RemoteDb, password: str, mysql_client: bool, timeout: int = 15) -> str:
    """Log in once with the mysql/mariadb client and return "version · user"."""
    import tempfile

    handle, name = tempfile.mkstemp(prefix="varsha-remote-", suffix=".cnf")
    cnf = Path(name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(f"[client]\nuser={option_value(server.user)}\npassword={option_value(password)}\n"
                         f"host={server.host}\nport={server.port}\nprotocol=TCP\n")
        cmd = [str(client), f"--defaults-extra-file={cnf.as_posix()}", f"--connect-timeout={timeout}"]
        if mysql_client:  # MySQL 8 client options
            cmd.append(f"--ssl-mode={'VERIFY_CA' if server.ssl_verify else 'REQUIRED' if server.ssl else 'DISABLED'}")
        else:  # MariaDB client options
            cmd.append("--ssl" if server.ssl else "--skip-ssl")
            if server.ssl_verify:
                cmd.append("--ssl-verify-server-cert")
        if server.ssl_ca:
            cmd.append(f"--ssl-ca={Path(server.ssl_ca).as_posix()}")
        cmd += ["-N", "-e", "SELECT VERSION(), CURRENT_USER()"]
        result = proc_utils.run_quiet(cmd, timeout=timeout + 10)
    except subprocess.TimeoutExpired:
        raise ServiceError(f"No answer from {server.host}:{server.port} within {timeout}s. Check the endpoint "
                           "and that the RDS security group allows your IP address.") from None
    finally:
        cnf.unlink(missing_ok=True)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip().splitlines()
        detail = next((line for line in message if "ERROR" in line), message[-1] if message else "unknown error")
        raise ServiceError(f"Login to {server.name} failed: {detail}{_hint(detail)}")
    version, _, user = result.stdout.strip().partition("\t")
    return f"Connected: {version} as {user}"
