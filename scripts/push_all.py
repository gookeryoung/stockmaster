"""Push the current branch (and tags) to every configured git remote.

Kept as a script rather than an inline Make recipe so the output encoding is
handled by the same console-encoding logic as the rest of the project: inline
Chinese in a Makefile is the original source of the mojibake this project fixed.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.console import prefer_console_encoding


def _run(argv: list[str]) -> int:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout)
    return result.returncode


def main() -> int:
    prefer_console_encoding()

    remotes = subprocess.run(["git", "remote"], capture_output=True, text=True, check=True).stdout.split()
    if not remotes:
        print("[push] 未配置任何 git remote。")
        return 1

    branch = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()

    failures = 0
    for remote in remotes:
        print(f"[push] 推送到 {remote}（分支 {branch} + tags）...", flush=True)
        if _run(["git", "push", remote, branch]) != 0 or _run(["git", "push", remote, "--tags"]) != 0:
            failures += 1

    if failures:
        print(f"[push] {failures} 个远程推送失败。")
        return 1
    print(f"[push] 完成：{', '.join(remotes)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
