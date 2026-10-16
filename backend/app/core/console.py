"""Console output encoding handling.

Why this is not auto-detected
-----------------------------
The obvious approach — ask Windows what code page the console uses — is unreliable.
Measured on this machine, three signals disagree:

    chcp                        -> 936      (the console's real code page)
    GetConsoleOutputCP()        -> 65001    (wrong)
    sys.stdout.encoding         -> gbk      (Python's negotiation)

An earlier version trusted ``GetConsoleOutputCP()``; another trusted the stream
encoding. Each was right on one machine and wrong on another, so Chinese output
flip-flopped between correct and mojibake. The resolution is to stop guessing:

1. **Explicit override** — honour ``SM_CONSOLE_ENCODING`` for genuine exceptions
   (a legacy GBK-only console, or a CI log with a known encoding).
2. **Otherwise UTF-8, always** — it renders correctly in the terminal actually in
   use, and it is what modern terminals, editors and log viewers expect. Having one
   predictable answer also removes the class of bug this area kept producing.

Use :func:`explain_console_encoding` (via `stockmaster doctor`) to see every signal
when something does look wrong.
"""

from __future__ import annotations

import contextlib
import ctypes
import locale
import os
import sys

__all__ = [
    "detect_console_encoding",
    "explain_console_encoding",
    "prefer_console_encoding",
]

_UTF8_CP = 65001
_UTF8 = "utf-8"


def _console_output_cp() -> int | None:
    """Code page of the console attached to this process, or None if detached.

    ``GetConsoleOutputCP`` returns 0 when there is no attached console, which is
    the reliable signal for "output is redirected / piped".
    """
    if sys.platform != "win32":
        return None
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        codepage = int(kernel32.GetConsoleOutputCP())
        return codepage or None
    except Exception:
        return None


#: Our own output is always UTF-8. This is a deliberate, fixed choice rather than a
#: runtime guess: every available signal disagrees on some machine, so detection
#: produced flip-flopping behaviour. Measured on this host:
#:
#:     chcp                        -> 936
#:     GetConsoleOutputCP()        -> 65001
#:     sys.stdout.encoding         -> gbk
#:
#: UTF-8 renders correctly in the terminal actually in use, and it is also what
#: modern terminals, editors and CI logs assume. Set ``SM_CONSOLE_ENCODING`` to
#: override for a genuinely GBK-only console.
_DEFAULT_ENCODING = _UTF8


def _console_input_cp() -> int | None:
    """Console *input* code page, for diagnostics.

    Differs from the output code page on this host (936 vs 65001), which is a useful
    clue when diagnosing encoding confusion.
    """
    if sys.platform != "win32":
        return None
    try:
        codepage = int(ctypes.windll.kernel32.GetConsoleCP())  # type: ignore[attr-defined]
        return codepage or None
    except Exception:
        return None


def detect_console_encoding() -> str:
    """Encoding used for our own console output.

    Fixed to UTF-8 unless ``SM_CONSOLE_ENCODING`` overrides it. See
    :data:`_DEFAULT_ENCODING` for why this is not auto-detected.
    """
    override = os.environ.get("SM_CONSOLE_ENCODING", "").strip()
    return override or _DEFAULT_ENCODING


def explain_console_encoding() -> dict:
    """Diagnostics for `doctor`.

    Reports every signal alongside the chosen encoding, because they conflict on
    real machines and that is confusing to debug: the obvious API call
    (``GetConsoleOutputCP``) disagreed with both `chcp` and Python's own stream
    encoding on this host.
    """
    return {
        "sm_console_encoding_override": os.environ.get("SM_CONSOLE_ENCODING", "") or None,
        "selected": detect_console_encoding(),
        "get_console_output_cp": _console_output_cp(),
        "console_input_cp": _console_input_cp(),
        "stdout_encoding": getattr(sys.stdout, "encoding", None),
        "stderr_encoding": getattr(sys.stderr, "encoding", None),
        "locale_encoding": locale_encoding(),
        "stdout_isatty": bool(getattr(sys.stdout, "isatty", lambda: False)()),
        "note": "signals may disagree; output is fixed to UTF-8 for consistency",
    }


def prefer_console_encoding() -> str:
    """Reconfigure stdout/stderr for the detected encoding.

    Returns the encoding selected. Streams that cannot be reconfigured (for
    example under pytest capture) are left untouched.
    """
    target = detect_console_encoding()
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        with contextlib.suppress(ValueError, OSError, LookupError):
            reconfigure(encoding=target, errors="replace")
    return target


def locale_encoding() -> str:
    """The process locale encoding (diagnostics and tests)."""
    try:
        return locale.getpreferredencoding(False) or _UTF8
    except Exception:
        return _UTF8
