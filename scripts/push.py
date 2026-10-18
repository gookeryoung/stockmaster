"""Push the current branch (and tags) to every configured git remote.

``make push`` used to inline a dense ``python -c`` one-liner that called
``git push <remote>`` without an explicit branch. That only works for the remote
that has an upstream configured: the second remote fails with "current branch has
no upstream branch" and tags are never pushed. This script makes the intent
explicit — current branch name + ``--tags`` pushed to every remote in
``git remote`` — without rewriting the branch's upstream.
"""

from __future__ import annotations

import argparse
import subprocess
import sys


def _capture(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], text=True, capture_output=True, check=False)


def _configure_console() -> None:
    """Keep non-UTF-8 consoles/redirects from raising on Chinese output.

    Mirrors the convention used by the other Makefile targets: replace rather
    than raising, so a push never fails merely because of console encoding.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


def main() -> int:
    _configure_console()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只演练 git push，不实际修改远程")
    args = parser.parse_args()

    branch_result = _capture("rev-parse", "--abbrev-ref", "HEAD")
    if branch_result.returncode != 0:
        print(f"无法读取当前分支：{branch_result.stderr.strip()}", file=sys.stderr)
        return 1
    branch = branch_result.stdout.strip()
    if branch in {"", "HEAD"}:
        print("当前处于 detached HEAD，无法推送；请先切回一个分支。", file=sys.stderr)
        return 1

    remotes_result = _capture("remote")
    if remotes_result.returncode != 0:
        print(f"无法读取 git remote：{remotes_result.stderr.strip()}", file=sys.stderr)
        return 1
    remotes = remotes_result.stdout.split()
    if not remotes:
        print("没有配置任何 git remote，无法推送。", file=sys.stderr)
        return 1

    failures: list[str] = []
    for remote in remotes:
        command = ["git", "push"]
        if args.dry_run:
            command.append("--dry-run")
        command += [remote, branch, "--tags"]
        print(f"[push] {' '.join(command)}", flush=True)
        # 子进程继承 stdout/stderr，SSH/凭据提示与远端输出能原样看到。
        if subprocess.run(command, check=False).returncode != 0:
            failures.append(remote)

    if failures:
        print(f"[push] 失败：{', '.join(failures)}；请检查 SSH 凭据/权限后重试。", file=sys.stderr)
        return 1

    action = "演练完成" if args.dry_run else "推送完成"
    print(f"[push] {action}：{branch} + tags → {', '.join(remotes)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
