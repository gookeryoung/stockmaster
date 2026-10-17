"""Render a `make help` listing from `## ` comments in Makefiles.

Extracted from the Makefile because inlining this Python in a recipe makes the
recipe vulnerable to shell quote mangling (Windows `make` runs recipes through
`sh`, which strips and reinterprets quotes) — the failure mode is an opaque
"CreateProcess ... failed" error.

Usage:
    python scripts/make_help.py Makefile [more.mk ...]
    python scripts/make_help.py --print-encoding    # probe the console encoding
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from stockmaster.core.console import detect_console_encoding, prefer_console_encoding

# A target line may declare several names ("build b:") and an optional `## help`.
_TARGET_RE = re.compile(r"^([a-zA-Z][\w -]*):.*?##\s*(.*)$")


def collect(paths: list[str]) -> list[tuple[str, str]]:
    """Return ``(target_names, description)`` pairs in file order."""
    entries: list[tuple[str, str]] = []
    for path in paths:
        try:
            with Path(path).open(encoding="utf-8") as handle:
                for line in handle:
                    match = _TARGET_RE.match(line)
                    if match:
                        entries.append((match.group(1).strip(), match.group(2).strip()))
        except OSError as exc:
            print(f"make_help: 无法读取 {path}: {exc}", file=sys.stderr)
    return entries


def main(argv: list[str]) -> int:
    # `--print-encoding` lets the Makefile fix the console encoding at parse time
    # (used by recipes whose output travels through make's own pipe) without
    # embedding inline Python that the shell would mangle.
    if "--print-encoding" in argv[1:]:
        print(detect_console_encoding())
        return 0

    # Match the terminal's encoding, otherwise `make help` garbles on a GBK console.
    prefer_console_encoding()

    entries = collect(argv[1:] or ["Makefile"])
    if not entries:
        print("未找到带 ## 说明的目标", file=sys.stderr)
        return 1

    width = max(len(name) for name, _ in entries)
    for name, description in entries:
        print(f"  {name:<{width}}  {description}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
