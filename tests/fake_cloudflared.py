"""Imitates `cloudflared tunnel --url ...` output for TunnelManager tests.

    fake_cloudflared.py --mode ok|fail|silent|die-later|url-only  tunnel --url U --http-host-header H ...
"""

from __future__ import annotations

import argparse
import sys
import time


def say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="ok")
    parser.add_argument("--url")
    parser.add_argument("--http-host-header")
    args, _rest = parser.parse_known_args()

    say("2026-11-04T08:00:00Z INF Thank you for trying Cloudflare Tunnel.")
    say(f"2026-11-04T08:00:00Z INF origin={args.url} host={args.http_host_header}")
    if args.mode == "fail":
        say('2026-11-04T08:00:01Z ERR failed to request quick Tunnel: error="no network"')
        return 1
    if args.mode == "silent":
        time.sleep(600)
        return 0
    say("2026-11-04T08:00:01Z INF +----------------------------------------------------+")
    say("2026-11-04T08:00:01Z INF |  https://quiet-river-demo-test.trycloudflare.com   |")
    say("2026-11-04T08:00:01Z INF +----------------------------------------------------+")
    if args.mode == "url-only":  # link printed but never registered
        time.sleep(600)
        return 0
    say("2026-11-04T08:00:02Z INF Registered tunnel connection connIndex=0 location=bom08")
    if args.mode == "die-later":
        time.sleep(1)
        return 2
    time.sleep(600)
    return 0


if __name__ == "__main__":
    sys.exit(main())
