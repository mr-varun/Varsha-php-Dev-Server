"""Port checks: is a TCP port free, and if not, which process holds it?"""

from __future__ import annotations

import logging
import socket
from collections.abc import Iterable
from dataclasses import dataclass

import psutil

logger = logging.getLogger(__name__)

WSAEACCES = 10013  # "forbidden by its access permissions": a Windows excluded port range
SYSTEM_PID = 4


@dataclass(frozen=True)
class PortOwner:
    port: int
    pid: int | None = None
    process_name: str | None = None
    reserved: bool = False  # inside a Windows excluded port range


def _process_name(pid: int | None) -> str | None:
    if not pid:
        return None
    try:
        return psutil.Process(pid).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def find_listeners(ports: Iterable[int]) -> dict[int, PortOwner]:
    """Map each port that has a listening TCP socket (IPv4 or IPv6) to its owner."""
    wanted = set(ports)
    if not wanted:
        return {}
    try:
        connections = psutil.net_connections(kind="tcp")
    except psutil.AccessDenied:
        logger.warning("Not allowed to list network connections; falling back to bind tests")
        return {}
    owners: dict[int, PortOwner] = {}
    for conn in connections:
        if conn.status != psutil.CONN_LISTEN or not conn.laddr or conn.laddr.port not in wanted:
            continue
        if conn.laddr.port not in owners:
            owners[conn.laddr.port] = PortOwner(conn.laddr.port, conn.pid, _process_name(conn.pid))
    return owners


def _bind_error(port: int) -> int | None:
    """Try to bind the port on both the wildcard and the loopback address.

    Returns None if both binds work, otherwise the Windows socket error code.
    Gotchas:
    - No SO_REUSEADDR: on Windows it allows binding a port another process already uses,
      which would make this test always pass.
    - No SO_EXCLUSIVEADDRUSE either: it fails on ports that only have TIME_WAIT leftovers,
      which would report a false "busy" right after stopping a service.
    - We bind without listen(), so Windows Firewall shows no prompt.
    """
    for host in ("0.0.0.0", "127.0.0.1"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, port))
        except OSError as exc:
            return getattr(exc, "winerror", None) or exc.errno or -1
        finally:
            sock.close()
    return None


def check_ports(ports: Iterable[int]) -> list[PortOwner]:
    """Return an owner for every port that is NOT free. An empty list means all are free."""
    unique = list(dict.fromkeys(ports))
    listeners = find_listeners(unique)
    busy: list[PortOwner] = []
    for port in unique:
        if port in listeners:
            busy.append(listeners[port])
            continue
        error = _bind_error(port)
        if error is not None:
            busy.append(PortOwner(port, reserved=error == WSAEACCES))
    return busy


def listening(ports: Iterable[int]) -> bool:
    wanted = set(ports)
    return wanted.issubset(find_listeners(wanted))


def describe_owner(owner: PortOwner) -> str:
    if owner.reserved:
        return (
            f"port {owner.port} is reserved by Windows (an excluded port range, often from "
            "Hyper-V, WSL or Docker). Check with: netsh interface ipv4 show excludedportrange "
            "protocol=tcp"
        )
    if owner.pid is None:
        return f"port {owner.port} is already in use by another program"
    name = owner.process_name or "an unknown process"
    text = f"port {owner.port} is in use by {name} (PID {owner.pid})"
    if owner.pid == SYSTEM_PID:
        text += (
            ". PID 4 is Windows itself: usually IIS / http.sys (World Wide Web Publishing "
            "Service) or another web server registered with http.sys"
        )
    return text
