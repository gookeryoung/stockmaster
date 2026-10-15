"""Foreground dev orchestrator: backend + frontend in *this* console.

Design notes
------------
Modelled on the cndb project's approach (`uv run cndb dev`): one foreground process
owning both children, so Ctrl+C stops everything together and there are no separate
console windows to hunt down and close.

- Child stdio is **inherited**, so interleaved logs land in this terminal.
- Children get their own process group, and shutdown signals the whole *tree* — the
  backend runs under `uvicorn --reload`, which is a reloader parent plus a server
  child, so signalling only the direct child orphans the grandchild holding the port.
- Ports are checked up front, so a conflict is a clear message rather than a bind
  error after both processes have started.
- Signal handling is owned by the caller: ``signal.signal()`` only works on the main
  thread, so installing it here would silently do nothing when hosted, and would make
  shutdown untestable. We poll an injected event instead.

Platform facts measured on this host (each invalidated an earlier assumption):

* ``connect_ex()`` cannot probe a port on Windows — it returns 10035
  (WSAEWOULDBLOCK) for *both* listening and unbound ports. A listening port accepts a
  ``connect()`` in ~0.00s while an unbound one blocks until the timeout, so a
  completed connection is the signal used here.
* ``taskkill /PID x /T`` without ``/F`` cannot stop Python processes (measured exit
  128: "can only be terminated forcefully"); a forced tree kill is required.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from app.core.logging import get_logger

log = get_logger(__name__)

__all__ = ["find_pnpm", "port_in_use", "run_dev"]

#: Long enough that a listening socket (which accepts immediately) is clearly
#: distinguishable from an unbound one, which blocks until the timeout expires.
PORT_PROBE_TIMEOUT = 1.0

#: Grace period before escalating to a forced tree kill.
GRACEFUL_EXIT_SECONDS = 6.0


def port_in_use(host: str, port: int) -> bool:
    """Whether something is already listening on ``host:port``.

    Uses ``connect()`` rather than ``connect_ex()``: on Windows ``connect_ex`` returns
    WSAEWOULDBLOCK (10035) for an unbound port *and* for a listening one, so no
    return-code mapping can tell them apart. Timing is what distinguishes them.
    """
    target = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(PORT_PROBE_TIMEOUT)
        try:
            sock.connect((target, port))
        except OSError:
            # Refused, timed out or unreachable: nothing is serving there.
            return False
        return True


def _find_project_root(start: Path | None = None) -> Path:
    """Locate the repository root by marker file.

    A fixed ``parents[n]`` index silently breaks when the module moves, and a wrong
    root makes the frontend dependency check inspect the wrong directory — which is
    exactly how this bug surfaced (it reported missing dependencies that were in fact
    installed).
    """
    here = (start or Path(__file__).resolve()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file() and (candidate / "frontend").is_dir():
            return candidate
    # Fall back to the nearest pyproject.toml so we still start something sensible.
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    return here


def find_pnpm() -> str | None:
    """Locate a launchable pnpm.

    On Windows ``pnpm`` resolves to ``pnpm.ps1``, which ``CreateProcess`` cannot
    execute; the ``.cmd`` shim is the runnable entry point.
    """
    for name in ("pnpm.cmd", "pnpm", "pnpm.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _terminate_tree(pid: int) -> None:
    """Ask a process *and its descendants* to exit.

    Needed because the backend runs under `uvicorn --reload`, which spawns a reloader
    and a server process; signalling only the direct child leaves those grandchildren
    alive holding the listening socket.
    """
    if os.name == "nt":
        # Best-effort graceful request. Python processes have no message loop, so this
        # usually cannot stop them (measured exit 128); the caller escalates to
        # _kill_tree once the grace period expires.
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, check=False)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)


def _kill_tree(pid: int) -> None:
    """Force-kill a process and its descendants."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGKILL)


def _pop_child_env() -> dict[str, str]:
    """Child environment, forcing UTF-8 stdio.

    Both children emit Chinese output; without this they would encode with the console
    code page and mojibake in a UTF-8 terminal.
    """
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")
    return env


def run_dev(
    *,
    host: str = "127.0.0.1",
    port: int = 8770,
    frontend_port: int = 5173,
    with_frontend: bool = True,
    reload: bool = True,
    project_root: Path | None = None,
    stop_event: threading.Event | None = None,
    max_seconds: float | None = None,
) -> int:
    """Run the backend (and optionally Vite) until stopped.

    Stops when ``stop_event`` is set, when ``max_seconds`` elapses, or when a child
    exits on its own. Returns 0 on a clean shutdown and non-zero if a child died.
    """
    root = project_root or _find_project_root()
    frontend_dir = root / "frontend"

    # --- preflight ----------------------------------------------------------
    if port_in_use(host, port):
        print(
            f"[dev] 端口 {port} 已被占用。请先执行 make dev-stop，或改用其它端口：make dev DEV_PORT=9000",
            file=sys.stderr,
        )
        return 1

    pnpm: str | None = None
    if with_frontend:
        if port_in_use(host, frontend_port):
            print(
                f"[dev] 端口 {frontend_port} 已被占用。请先执行 make dev-stop，"
                f"或改用其它端口：make dev FRONTEND_PORT=5174",
                file=sys.stderr,
            )
            return 1
        if not (frontend_dir / "node_modules").is_dir():
            print("[dev] 前端依赖未安装。请先执行：make frontend-sync", file=sys.stderr)
            return 1
        pnpm = find_pnpm()
        if not pnpm:
            print("[dev] 未找到 pnpm，请先安装：npm install -g pnpm", file=sys.stderr)
            return 1

    children: list[tuple[str, subprocess.Popen]] = []
    env = _pop_child_env()

    def spawn(label: str, argv: list[str], cwd: Path) -> None:
        print(f"[dev] 启动{label}： {' '.join(argv)}", flush=True)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd),
                env=env,
                # Inherit stdio so both children log into this console.
                stdin=subprocess.DEVNULL,
                stdout=None,
                stderr=None,
                # Own process group, so shutdown can signal the whole tree.
                start_new_session=(os.name != "nt"),
                creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            )
        except OSError as exc:
            print(f"[dev] 无法启动{label}：{exc}", file=sys.stderr)
            raise
        children.append((label, proc))

    stopping = {"flag": False}

    def shutdown() -> None:
        if stopping["flag"]:
            return
        stopping["flag"] = True
        print("\n[dev] 正在停止 ...", flush=True)
        for _label, proc in children:
            if proc.poll() is None:
                _terminate_tree(proc.pid)

        deadline = time.monotonic() + GRACEFUL_EXIT_SECONDS
        for label, proc in children:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                # Expected on Windows, where the graceful pass cannot stop a Python
                # process; escalate so no child survives holding a port.
                print(
                    f"[dev] {label}未在 {GRACEFUL_EXIT_SECONDS:.0f}s 内退出，强制结束进程树。",
                    flush=True,
                )
                _kill_tree(proc.pid)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=10)
        print("[dev] 已停止。", flush=True)

    exit_code = 0
    started_at = time.monotonic()
    try:
        spawn(
            "后端",
            [
                "uv",
                "run",
                "stockmaster",
                "serve",
                "--host",
                host,
                "--port",
                str(port),
                *(["--reload"] if reload else []),
            ],
            root,
        )
        if with_frontend:
            spawn(
                "前端",
                [
                    pnpm,
                    "exec",
                    "vite",
                    "--host",
                    host,
                    "--port",
                    str(frontend_port),
                    "--strictPort",
                ],
                frontend_dir,
            )

        print("", flush=True)
        print(f"[dev] 后端 API : http://{host}:{port}/docs", flush=True)
        if with_frontend:
            print(f"[dev] 前端界面 : http://{host}:{frontend_port}", flush=True)
        print("[dev] Ctrl+C 一并停止两个服务", flush=True)
        print("", flush=True)

        while True:
            if stopping["flag"]:
                break
            if stop_event is not None and stop_event.is_set():
                shutdown()
                break
            if max_seconds is not None and time.monotonic() - started_at >= max_seconds:
                print("\n[dev] 达到运行时长上限，正在停止 ...", flush=True)
                shutdown()
                break

            for label, proc in children:
                code = proc.poll()
                if code is not None:
                    if not stopping["flag"]:
                        print(
                            f"\n[dev] {label}已退出（退出码 {code}），正在停止其余进程。",
                            file=sys.stderr,
                            flush=True,
                        )
                        exit_code = code if code else 1
                    shutdown()
                    break
            else:
                time.sleep(0.25)
                continue
            break
    except KeyboardInterrupt:
        shutdown()
    finally:
        if not stopping["flag"]:
            shutdown()

    return exit_code
