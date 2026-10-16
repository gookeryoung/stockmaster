"""Console encoding tests.

These guard a Windows-specific failure that is easy to reintroduce and awkward to
notice in CI: emitting the wrong code page turns every Chinese label into mojibake.

The resolved design is deliberately *not* auto-detected. Three signals disagree on
this host (``chcp`` says 936, ``GetConsoleOutputCP()`` says 65001, Python's stream
says gbk), and the two detection strategies tried during development each broke a
different machine. Output is therefore fixed to UTF-8, with
``SM_CONSOLE_ENCODING`` as the escape hatch — one predictable answer instead of a
per-machine guess.
"""

from __future__ import annotations

import sys

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

    def test_default_is_utf8_regardless_of_console_codepage(self, monkeypatch):
        """The core decision: do not vary with the reported console code page.

        A 936 answer must NOT switch us to cp936 — that was the regression which
        produced mojibake, because the terminal in use decodes UTF-8 even though the
        console reports 936.
        """
        monkeypatch.delenv("SM_CONSOLE_ENCODING", raising=False)
        for codepage in (936, 65001, 1252, None):
            monkeypatch.setattr("app.core.console._console_output_cp", lambda cp=codepage: cp)
            assert detect_console_encoding() == "utf-8", f"codepage={codepage}"

    def test_override_beats_the_default(self, monkeypatch):
        monkeypatch.setenv("SM_CONSOLE_ENCODING", "cp936")
        assert detect_console_encoding() == "cp936"


class TestExplainConsoleEncoding:
    def test_reports_every_signal(self, monkeypatch):
        """`doctor` needs the raw signals, because they genuinely disagree."""
        monkeypatch.delenv("SM_CONSOLE_ENCODING", raising=False)
        from app.core.console import explain_console_encoding

        info = explain_console_encoding()
        for key in (
            "selected",
            "get_console_output_cp",
            "console_input_cp",
            "stdout_encoding",
            "stderr_encoding",
            "locale_encoding",
            "stdout_isatty",
        ):
            assert key in info, key
        assert info["selected"] == "utf-8"


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
