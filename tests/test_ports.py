from __future__ import annotations

import os
import socket

from conftest import free_port
from varsha.services.ports import PortOwner, check_ports, describe_owner, listening


def test_free_port_is_free() -> None:
    port = free_port()
    assert check_ports([port]) == []
    assert not listening([port])


def test_busy_port_names_the_owner() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = sock.getsockname()[1]
        (owner,) = check_ports([port, port])  # duplicates are checked once
        assert owner.port == port
        assert owner.pid == os.getpid()
        assert owner.process_name and "python" in owner.process_name.lower()
        assert listening([port])
        assert f"PID {os.getpid()}" in describe_owner(owner)


def test_describe_system_and_reserved() -> None:
    assert "http.sys" in describe_owner(PortOwner(80, 4, "System"))
    assert "excludedportrange" in describe_owner(PortOwner(80, reserved=True))
    assert "another program" in describe_owner(PortOwner(80))
