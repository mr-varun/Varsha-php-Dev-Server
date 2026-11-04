"""A tiny TCP server that stands in for a real service in manager tests.

    fake_service.py --port N [--ignore-stop] [--exit-immediately CODE]

Sending b"stop" to the port makes it exit 0 (the "graceful stop command"), unless --ignore-stop.
"""

from __future__ import annotations

import argparse
import socket
import sys


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--ignore-stop", action="store_true")
    parser.add_argument("--exit-immediately", type=int, default=None)
    args = parser.parse_args()

    if args.exit_immediately is not None:
        sys.stdout.write("fake service failing on purpose\n")
        sys.stdout.flush()
        return args.exit_immediately

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", args.port))
    server.listen()
    sys.stdout.write(f"fake service listening on {args.port}\n")
    sys.stdout.flush()
    while True:
        conn, _addr = server.accept()
        with conn:
            data = conn.recv(64)
        if data.strip() == b"stop" and not args.ignore_stop:
            sys.stdout.write("fake service stopping\n")
            sys.stdout.flush()
            return 0


if __name__ == "__main__":
    sys.exit(main())
