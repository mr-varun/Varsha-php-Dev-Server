"""Edit the Windows hosts file, but only between varsha's markers.

Everything outside `# varsha-start` / `# varsha-end` (Laragon's lines, the user's own entries)
is preserved byte for byte. The pure functions here run unprivileged (to check whether an
update is needed); the actual write happens in the elevated megha helper.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path

START = "# varsha-start"
END = "# varsha-end"
LOOPBACKS = ("127.0.0.1", "::1")
HOSTNAME_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")


def hosts_path() -> Path:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return Path(root) / "System32" / "drivers" / "etc" / "hosts"


def read_text(path: Path) -> str:
    # latin-1 maps every byte to one character, so whatever encoding the file has (ANSI,
    # UTF-8, odd bytes from other tools) survives a read/write round trip unchanged.
    return path.read_bytes().decode("latin-1")


def write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("latin-1"))


def _newline(text: str) -> str:
    return "\r\n" if "\r\n" in text or not text else "\n"


def split(text: str) -> tuple[str, str | None, str]:
    """(before, block-or-None, after). The block includes both marker lines."""
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.strip() == START), None)
    if start is None:
        return text, None, ""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == END), None)
    if end is None:  # damaged block: treat the rest of the file as the block
        end = len(lines) - 1
    return "".join(lines[:start]), "".join(lines[start:end + 1]), "".join(lines[end + 1:])


def mapped_outside(text: str) -> set[str]:
    """Names already pointed at a loopback address outside varsha's block (e.g. by Laragon)."""
    before, _block, after = split(text)
    names: set[str] = set()
    for line in (before + after).splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[0] in LOOPBACKS:
            names.update(name.lower() for name in parts[1:])
    return names


def entries_for(names: Iterable[str], text: str) -> list[str]:
    """Hosts names varsha should add: valid, de-duplicated, not already mapped elsewhere."""
    skip = mapped_outside(text)
    wanted: list[str] = []
    for name in names:
        name = name.lower()
        if HOSTNAME_RE.match(name) and name not in skip and name not in wanted:
            wanted.append(name)
    return wanted


def render_block(names: Iterable[str], newline: str = "\r\n") -> str:
    names = list(names)
    lines = [START, "# Managed by varsha: edits between these markers are overwritten."]
    for name in names:
        lines.append(f"127.0.0.1\t{name}")
        lines.append(f"::1\t{name}")
    lines.append(END)
    return newline.join(lines) + newline


def apply_block(text: str, names: Iterable[str]) -> str:
    """The hosts file with varsha's block replaced (or added, or removed when names is empty)."""
    newline = _newline(text)
    before, block, after = split(text)
    names = list(names)
    new_block = render_block(names, newline) if names else ""
    if block is None:
        if not new_block:
            return text
        if before and not before.endswith(("\n", "\r")):
            before += newline
        return before + new_block
    return before + new_block + after


def current_names(text: str) -> list[str]:
    _before, block, _after = split(text)
    if block is None:
        return []
    names: list[str] = []
    for line in block.splitlines():
        parts = line.split("#", 1)[0].split()
        if len(parts) >= 2 and parts[0] == "127.0.0.1":
            names.extend(parts[1:])
    return names


def is_in_sync(text: str, names: Iterable[str]) -> bool:
    return current_names(text) == entries_for(names, text)
