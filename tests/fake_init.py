"""Stand-in for `mysqld --initialize` in tests: writes stdin into <data_dir>/initialised.txt.

    fake_init.py <data_dir> [--fail]
"""

from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    data_dir = Path(sys.argv[1])
    stdin = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "initialised.txt").write_bytes(stdin or b"no stdin")
    if "--fail" in sys.argv:
        sys.stdout.write("fake init failing on purpose\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
