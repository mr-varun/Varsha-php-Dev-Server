"""A tiny DNS server: *.<tld> -> 127.0.0.1 / ::1, everything else forwarded upstream.

Run as its own process (`python -m varsha.dns.server --port 53 --tld test`) so the service
manager treats it like any other service. Binding port 53 needs no admin rights on Windows
(there are no privileged ports); only routing Windows' lookups to it does, and that's megha's
job (an NRPT rule that sends just *.test here).
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import logging
import socket
import sys
import threading
import time
from collections.abc import Sequence

from dnslib import AAAA, QTYPE, RCODE, RR, A, DNSRecord
from dnslib.server import BaseResolver, DNSLogger, DNSServer

logger = logging.getLogger("varsha.dns")

TTL = 60


class TldResolver(BaseResolver):
    def __init__(self, tld: str, upstreams: Sequence[str], timeout: float = 3.0) -> None:
        self.suffix = "." + tld.strip(".").lower()
        self.upstreams = [u for u in upstreams if not u.startswith("127.") and u != "::1"]
        self.timeout = timeout

    def is_local(self, name: str) -> bool:
        name = name.rstrip(".").lower()
        return name.endswith(self.suffix) and name != self.suffix.lstrip(".")

    def resolve(self, request: DNSRecord, handler: object) -> DNSRecord:
        qname = str(request.q.qname)
        qtype = QTYPE[request.q.qtype]
        if self.is_local(qname):
            reply = request.reply()
            if qtype in ("A", "ANY"):
                reply.add_answer(RR(qname, QTYPE.A, rdata=A("127.0.0.1"), ttl=TTL))
            if qtype in ("AAAA", "ANY"):
                reply.add_answer(RR(qname, QTYPE.AAAA, rdata=AAAA("::1"), ttl=TTL))
            return reply  # other types: empty NOERROR answer, the name exists
        return self.forward(request)

    def forward(self, request: DNSRecord) -> DNSRecord:
        data = request.pack()
        for upstream in self.upstreams:
            try:
                return DNSRecord.parse(DNSRecord.parse(data).send(upstream, 53, timeout=self.timeout))
            except (OSError, socket.timeout, ValueError):
                logger.debug("Upstream %s failed", upstream)
        reply = request.reply()
        reply.header.rcode = RCODE.SERVFAIL
        return reply


def system_dns_servers() -> list[str]:
    """The DNS servers Windows itself uses (GetNetworkParams), loopback excluded."""
    if sys.platform != "win32":
        return []

    class IpAddrString(ctypes.Structure):
        pass

    IpAddrString._fields_ = [("Next", ctypes.POINTER(IpAddrString)), ("IpAddress", ctypes.c_char * 16),
                             ("IpMask", ctypes.c_char * 16), ("Context", ctypes.wintypes.DWORD)]

    class FixedInfo(ctypes.Structure):
        _fields_ = [("HostName", ctypes.c_char * 132), ("DomainName", ctypes.c_char * 132),
                    ("CurrentDnsServer", ctypes.POINTER(IpAddrString)), ("DnsServerList", IpAddrString),
                    ("NodeType", ctypes.c_uint), ("ScopeId", ctypes.c_char * 260),
                    ("EnableRouting", ctypes.c_uint), ("EnableProxy", ctypes.c_uint),
                    ("EnableDns", ctypes.c_uint)]

    iphlpapi = ctypes.WinDLL("iphlpapi")
    size = ctypes.c_ulong(0)
    iphlpapi.GetNetworkParams(None, ctypes.byref(size))
    buffer = ctypes.create_string_buffer(size.value)
    if iphlpapi.GetNetworkParams(buffer, ctypes.byref(size)) != 0:
        return []
    info = ctypes.cast(buffer, ctypes.POINTER(FixedInfo)).contents
    servers: list[str] = []
    node: IpAddrString | None = info.DnsServerList
    while node is not None:
        address = node.IpAddress.decode("ascii", "ignore").strip("\0")
        if address and not address.startswith("127.") and address not in servers:
            servers.append(address)
        node = node.Next.contents if node.Next else None
    return servers


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="varsha-dns")
    parser.add_argument("--port", type=int, default=53)
    parser.add_argument("--address", default="127.0.0.1")
    parser.add_argument("--tld", default="test")
    parser.add_argument("--upstream", action="append", default=[])
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)

    upstreams = args.upstream or system_dns_servers() or ["1.1.1.1", "8.8.8.8"]
    resolver = TldResolver(args.tld, upstreams)
    logger.info("varsha DNS on %s:%d: *.%s -> 127.0.0.1, others -> %s", args.address, args.port,
                args.tld, ", ".join(resolver.upstreams) or "(none)")
    # Both UDP (normal lookups) and TCP (large answers; also what varsha's port check sees).
    quiet = DNSLogger(log="-request,-reply,-truncated,-data", prefix=False)  # errors only
    servers = [DNSServer(resolver, port=args.port, address=args.address, tcp=tcp, logger=quiet)
               for tcp in (False, True)]
    for server in servers:
        server.start_thread()
    stop = threading.Event()
    try:
        while not stop.wait(1) and all(s.isAlive() for s in servers):
            pass
    except KeyboardInterrupt:
        pass
    for server in servers:
        server.stop()
    time.sleep(0.1)
    return 1 if not stop.is_set() and not all(s.isAlive() for s in servers) else 0


if __name__ == "__main__":
    sys.exit(main())
