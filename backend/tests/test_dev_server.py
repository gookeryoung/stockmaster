"""Tests for the foreground dev orchestrator.

The behaviours pinned here are the ones that were actually wrong while building it:

1. **Project root discovery.** A fixed `parents[n]` index resolved to `F:\\Dev`
   instead of the repo root, so the frontend check reported missing dependencies
   that were in fact installed. Now marker-based.
2. **Whole-tree shutdown.** `Popen.terminate()` only signals the direct child. The
   backend runs under `uvicorn --reload` (reloader + server child), so the reloader
   was orphaned and kept the port bound. `_terminate_tree` must reach descendants.
3. **No signal handling inside `run_dev`.** `signal.signal()` only works on the main
   thread, so installing handlers there silently did nothing when hosted. Shutdown
   is driven by an injected event instead.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.dev_server import (
    _find_project_root,
    _kill_tree,
    _terminate_tree,
    find_pnpm,
    port_in_use,
    run_dev,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

#: Windows needs a forced kill, so allow the graceful attempt to expire first.
GRACEFUL_WAIT = 12.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestProjectRoot:
    def test_finds_repository_root(self):
        root = _find_project_root()
        assert (root / "pyproject.toml").is_file()
        assert (root / "frontend").is_dir()
        assert root == REPO_ROOT

    def test_independent_of_module_location(self):
        r"""Discovery must not rely on a fixed parents[] index.

        Every start point *inside* the repo must resolve to the repo root, which is
        exactly what a magic index got wrong (it returned ``F:\Dev``).
        """
        starts = [
            Path(__file__).resolve(),  # the test file itself
            REPO_ROOT / "backend" / "app" / "dev_server.py",  # the module itself
            REPO_ROOT / "backend",  # the backend directory
            REPO_ROOT / "scripts",  # a sibling directory
            REPO_ROOT,  # already the root
        ]
        for start in starts:
            found = _find_project_root(start)
            assert found == REPO_ROOT, f"start={start} -> {found}"
            assert (found / "pyproject.toml").is_file()
            assert (found / "frontend").is_dir()

    def test_ancestor_without_markers_falls_back_to_start(self):
        """With no marker anywhere up the tree, discovery returns the start point.

        It deliberately does *not* search siblings, so running from a directory
        outside the repo cannot silently pick a neighbouring project. Callers that
        need a specific root pass ``project_root`` explicitly.
        """
        assert not (REPO_ROOT.parent / "pyproject.toml").exists(), (
            "test assumes the parent directory is not itself a project"
        )
        found = _find_project_root(REPO_ROOT.parent)
        assert found == REPO_ROOT.parent.resolve()

    def test_falls_back_to_nearest_pyproject(self, tmp_path):
        """Without a frontend directory, fall back to the nearest pyproject."""
        (tmp_path / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        nested = tmp_path / "a" / "b"
        nested.mkdir(parents=True)
        assert _find_project_root(nested) == tmp_path


class TestPortInUse:
    def test_detects_a_listening_socket(self):
        port = _free_port()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(1)
        try:
            assert port_in_use("127.0.0.1", port) is True
        finally:
            server.close()

    def test_reports_free_port(self):
        port = _free_port()
        assert port_in_use("127.0.0.1", port) is False

    def test_wildcard_host_is_probed_on_loopback(self):
        """0.0.0.0 cannot be connected to directly; probe 127.0.0.1 instead."""
        port = _free_port()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("0.0.0.0", port))
        server.listen(1)
        try:
            assert port_in_use("0.0.0.0", port) is True
        finally:
            server.close()


class TestFindPnpm:
    def test_returns_a_runnable_path_or_none(self):
        """On Windows `pnpm` resolves to pnpm.ps1, which CreateProcess cannot run."""
        found = find_pnpm()
        if found is None:
            pytest.skip("pnpm not installed in this environment")
        assert Path(found).exists()
        if sys.platform == "win32":
            assert found.lower().endswith((".cmd", ".exe")), found


class TestTerminateTree:
    """The core regression: descendants must be killed, not orphaned."""

    # Note: an earlier `test_terminate_tree_stops_a_real_child` spawned a synthetic
    # child and asserted its bind was observable. It proved unreliable inside pytest
    # (the child bound and held the port when driven manually, but the assertion
    # regularly saw nothing), and it duplicated coverage. Tree termination is
    # verified below against the real `uvicorn --reload` process structure, which is
    # the shape that actually matters.

    def test_run_dev_releases_port_after_shutdown(self):
        """The real reloader tree must not outlive shutdown.

        `uvicorn --reload` runs a reloader parent plus a server child, so a released
        port is direct evidence the whole tree was terminated.
        """
        port = _free_port()
        code = run_dev(
            host="127.0.0.1",
            port=port,
            with_frontend=False,
            reload=True,
            project_root=REPO_ROOT,
            max_seconds=20,
        )
        assert code == 0

        deadline = time.time() + 20
        while time.time() < deadline and port_in_use("127.0.0.1", port):
            time.sleep(0.5)
        assert not port_in_use("127.0.0.1", port), "reloader child survived shutdown and kept the port bound"

    def test_stop_event_shuts_down_and_releases_the_port(self):
        """Setting the stop event must terminate the tree and free the port.

        This is the exact path the Ctrl+C handler drives in `_dev`: the handler only
        sets an event, and the orchestrator performs an ordered tree shutdown.
        """
        import threading

        port = _free_port()
        stop_event = threading.Event()
        result: dict = {}

        def run() -> None:
            result["code"] = run_dev(
                host="127.0.0.1",
                port=port,
                with_frontend=False,
                reload=True,
                project_root=REPO_ROOT,
                stop_event=stop_event,
            )

        worker = threading.Thread(target=run, daemon=True)
        worker.start()

        deadline = time.time() + 60
        while time.time() < deadline and not port_in_use("127.0.0.1", port):
            time.sleep(0.5)
        assert port_in_use("127.0.0.1", port), "backend never came up"

        stop_event.set()
        worker.join(timeout=45)
        assert not worker.is_alive(), "run_dev did not stop after the stop event"
        assert result.get("code") == 0

        deadline = time.time() + 20
        while time.time() < deadline and port_in_use("127.0.0.1", port):
            time.sleep(0.5)
        assert not port_in_use("127.0.0.1", port), "reloader child survived shutdown and kept the port bound"

    def test_kill_tree_is_idempotent_for_a_dead_process(self):
        """Killing an already-exited process must not raise."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait(timeout=30)
        _kill_tree(proc.pid)  # must not raise
        _terminate_tree(proc.pid)  # must not raise


class TestRunDevPreflight:
    def test_refuses_when_backend_port_is_taken(self):
        """A busy port must produce a clear message, not a late bind error."""
        from app.dev_server import run_dev

        port = _free_port()
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", port))
        server.listen(1)
        try:
            code = run_dev(
                host="127.0.0.1",
                port=port,
                with_frontend=False,
                reload=False,
                project_root=REPO_ROOT,
            )
        finally:
            server.close()

        assert code == 1
