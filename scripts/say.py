"""Print a message using the console's own encoding.

Used by the Makefile instead of a bare `echo`. Make reads recipe text as UTF-8
from the Makefile and hands those bytes to the shell, which on Windows writes them
through unchanged — so on a 936 (GBK) console every Chinese message becomes
mojibake. Going through Python lets the message be re-encoded to match the
terminal.

Usage:
    python scripts/say.py "消息文本"
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.console import prefer_console_encoding


def main(argv: list[str]) -> int:
    prefer_console_encoding()
    message = " ".join(argv[1:])
    if message:
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
