"""Console output encoding handling.

Why this is not simply "force UTF-8"
------------------------------------
On a Chinese Windows install the console code page is typically **936 (GBK)**.

* With stdout attached to that console, Python encodes as GBK and it renders
  correctly.
* Under `make`, stdout is a **pipe**. Python then uses the locale encoding while
  the *terminal* remains the thing that decodes — so emitting UTF-8 into a GBK
  console shows mojibake, and emitting GBK into a UTF-8 terminal does the same.

Blindly forcing UTF-8 therefore does not fix anything; it moves the breakage.
The rule used here:

1. **Explicit override** — honour ``SM_CONSOLE_ENCODING`` (lets a caller or CI
   force a choice, e.g. ``utf-8`` in a UTF-8 CI log).
2. **Interactive** — when a console is attached, use *its* code page, which is
   exactly what that console will decode correctly.
3. **Piped** — no way to interrogate the far end reliably, so use UTF-8, the
   cross-platform default that modern terminals and log viewers assume.
"""

from __future__ import annotations

import contextlib
import ctypes
import locale
import os
import sys

__all__ = ["detect_console_encoding", "prefer_console_encoding"]

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


def detect_console_encoding() -> str:
    """Encoding that the current output target can render."""
    override = os.environ.get("SM_CONSOLE_ENCODING", "").strip()
    if override:
        return override

    codepage = _console_output_cp()
    if codepage:
        # A UTF-8 console must keep the -8 suffix: the bare "cp65001" alias is
        # accepted by Python but "utf-8" is clearer and avoids edge cases.
        return _UTF8 if codepage == _UTF8_CP else f"cp{codepage}"

    # Piped or redirected: prefer UTF-8 for portability.
    return _UTF8


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
