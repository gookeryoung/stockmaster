"""Byte-level regression guard for console output encoding.

Unit tests on `detect_console_encoding()` cannot catch the real failure: the app can
select the "right" name and still emit bytes the terminal cannot decode. This test
runs the actual CLI in a subprocess and inspects the raw bytes.

Context: the user reported mojibake from `sync-daily --codes`, and two detection
strategies were each right on one machine and wrong on another. The guard is simply
"our stdout must be valid UTF-8", which is what the terminal in use decodes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"


def _run_cli(args: list[str], env_extra: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Run the CLI with a clean environment (notably no PYTHONIOENCODING)."""
    env = dict(os.environ)
    # A leftover PYTHONIOENCODING would mask the behaviour under test.
    env.pop("PYTHONIOENCODING", None)
    env.pop("SM_CONSOLE_ENCODING", None)
    env["PYTHONPATH"] = str(BACKEND)
    env["SM_DATA_DIR"] = str(Path(os.environ.get("TEMP", "/tmp")) / f"enc-{uuid.uuid4().hex}")
    if env_extra:
        env.update(env_extra)

    return subprocess.run(
        [sys.executable, "-m", "app.cli", *args],
        cwd=str(BACKEND),
        env=env,
        capture_output=True,
        check=False,
        timeout=180,
    )


class TestStdoutIsValidUtf8:
    def test_doctor_output_is_valid_utf8(self):
        """The simplest command that prints Chinese must emit decodable bytes."""
        result = _run_cli(["doctor", "--log-level", "ERROR"])
        assert result.stdout, "doctor produced no output"
        try:
            result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            pytest.fail(
                f"stdout is not valid UTF-8 (offset {exc.start}); a terminal "
                f"decoding UTF-8 would show mojibake: {result.stdout[:120]!r}"
            )

    def test_stats_output_is_valid_utf8(self):
        result = _run_cli(["stats", "--log-level", "ERROR"])
        try:
            result.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            pytest.fail(f"stats stdout is not valid UTF-8 (offset {exc.start})")

    def test_sync_daily_chinese_is_valid_utf8(self):
        """The exact command the user reported mojibake from.

        Runs with data providers forced offline (``SM_PROVIDER_OFFLINE``), so the
        Chinese preamble/progress lines are produced without any network access:
        the source being unreachable is irrelevant to the byte-level assertion, and
        a live fetch here made CI slow and network-dependent.
        """
        result = _run_cli(
            [
                "sync-daily",
                "--codes",
                "600519.SH",
                "--years",
                "1",
                "--workers",
                "1",
                "--retries",
                "0",
                "--log-level",
                "ERROR",
            ],
            {"SM_PROVIDER_OFFLINE": "true"},
        )
        combined = result.stdout + result.stderr
        assert combined, "sync-daily produced no output"
        try:
            combined.decode("utf-8")
        except UnicodeDecodeError as exc:
            pytest.fail(f"sync-daily output is not valid UTF-8 (offset {exc.start}); bytes: {combined[:120]!r}")


class TestEncodingOverrideStillWorks:
    def test_sm_console_encoding_utf8_keeps_valid_utf8(self):
        result = _run_cli(["doctor", "--log-level", "ERROR"], {"SM_CONSOLE_ENCODING": "utf-8"})
        result.stdout.decode("utf-8")  # must not raise

    def test_override_is_honoured_by_the_report(self):
        """A GBK override must be visible in doctor's reported encoding."""
        result = _run_cli(["doctor", "--log-level", "ERROR"], {"SM_CONSOLE_ENCODING": "cp936"})
        text = result.stdout.decode("utf-8", errors="replace")
        assert "cp936" in text, text
