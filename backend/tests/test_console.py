"""Console encoding tests.

These guard a Windows-specific failure that is easy to reintroduce and awkward to
notice in CI: emitting the wrong code page turns every Chinese label into
mojibake. The rule under test is "match the terminal, do not impose a favourite".
"""

from __future__ import annotations

import sys

import pytest

from app.core.console import (
    detect_console_encoding,
    locale_encoding,
    prefer_console_encoding,
)


class TestDetectConsoleEncoding:
    def test_explicit_override_wins(self, monkeypatch):
        monkeypatch.setenv("SM_CONSOLE_ENCODING", "cp932")
        assert detect_console_encoding() == "cp932"

    def test_override_is_trimmed(self, monkeypatch):
        monkeypatch.setenv("SM_CONSOLE_ENCODING", "  utf-8  ")
        assert detect_console_encoding() == "utf-8"

    def test_blank_override_is_ignored(self, monkeypatch):
        monkeypatch.setenv("SM_CONSOLE_ENCODING", "   ")
        # Falls through to detection; must still return something usable.
        assert detect_console_encoding()

    def test_returns_something_always(self, monkeypatch):
        monkeypatch.delenv("SM_CONSOLE_ENCODING", raising=False)
        result = detect_console_encoding()
        assert isinstance(result, str) and result

    def test_result_is_a_known_encoding(self, monkeypatch):
        """The returned name must be resolvable by Python, or printing would fail."""
        monkeypatch.delenv("SM_CONSOLE_ENCODING", raising=False)
        name = detect_console_encoding()
        # codecs.lookup raises LookupError for an unknown name.
        import codecs

        assert codecs.lookup(name)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows console API only")
    def test_utf8_console_reports_utf8(self, monkeypatch):
        monkeypatch.delenv("SM_CONSOLE_ENCODING", raising=False)
        monkeypatch.setattr("app.core.console._console_output_cp", lambda: 65001)
        assert detect_console_encoding() == "utf-8"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows console API only")
    def test_gbk_console_reports_cp936(self, monkeypatch):
        """A 936 console must get cp936, not UTF-8 — this is the regression guard."""
        monkeypatch.delenv("SM_CONSOLE_ENCODING", raising=False)
        monkeypatch.setattr("app.core.console._console_output_cp", lambda: 936)
        assert detect_console_encoding() == "cp936"

    def test_no_console_falls_back_to_utf8(self, monkeypatch):
        """Piped output (no attached console) should default to portable UTF-8."""
        monkeypatch.delenv("SM_CONSOLE_ENCODING", raising=False)
        monkeypatch.setattr("app.core.console._console_output_cp", lambda: None)
        assert detect_console_encoding() == "utf-8"


class TestPreferConsoleEncoding:
    def test_returns_the_selected_encoding(self, monkeypatch):
        monkeypatch.setenv("SM_CONSOLE_ENCODING", "utf-8")
        assert prefer_console_encoding() == "utf-8"

    def test_is_safe_when_streams_cannot_be_reconfigured(self, monkeypatch):
        """pytest capture replaces stdout with an object lacking reconfigure()."""

        class NoReconfigure:
            encoding = "utf-8"

        monkeypatch.setattr(sys, "stdout", NoReconfigure())
        monkeypatch.setattr(sys, "stderr", NoReconfigure())
        monkeypatch.setenv("SM_CONSOLE_ENCODING", "cp936")
        # Must not raise despite the streams being unconfigurable.
        assert prefer_console_encoding() == "cp936"

    def test_does_not_raise_on_bad_encoding_name(self, monkeypatch):
        monkeypatch.setenv("SM_CONSOLE_ENCODING", "definitely-not-an-encoding")
        # LookupError is suppressed; the function still reports what it chose.
        assert prefer_console_encoding() == "definitely-not-an-encoding"


class TestLocaleEncoding:
    def test_returns_a_usable_name(self):
        import codecs

        assert codecs.lookup(locale_encoding())
