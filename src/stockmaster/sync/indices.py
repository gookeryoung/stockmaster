"""Parsing helpers for index membership data.

Kept separate from :mod:`app.sync.sync_service` so the column-selection logic —
which is easy to get subtly wrong and silently breaks the default universe — can
be unit-tested directly.
"""

from __future__ import annotations

from stockmaster.core.errors import DataError
from stockmaster.core.logging import get_logger
from stockmaster.core.symbols import normalize

log = get_logger(__name__)

__all__ = ["parse_constituent_codes"]


def _is_index_code_column(name: str) -> bool:
    """True for the index's own code column (``指数代码`` / ``index code``)."""
    text = str(name)
    lowered = text.lower()
    return ("指数" in text and "代码" in text) or "index" in lowered


def parse_constituent_codes(df, index_digits: str) -> list[str]:
    """Extract constituent symbols from a CSI index-membership frame.

    Column selection is **name-first**, because content alone is ambiguous: the
    real frame is ``[日期, 指数代码, 指数名称, 指数英文名称, 成分券代码, ...]``,
    where ``指数代码`` also contains six-digit strings (``000300`` for 沪深300).
    Matching purely by content latches onto that column, and a bare ``000300``
    normalises to the non-existent ``000300.SZ`` — which silently emptied the
    default universe and disabled model training.

    Content matching remains as a fallback (excluding index-code-shaped values) so
    a renamed upstream column degrades gracefully rather than breaking outright.

    Args:
        df: the DataFrame returned by the index-constituent endpoint.
        index_digits: the index's own six-digit code, excluded from the result.

    Returns:
        Canonical symbols (e.g. ``600519.SH``); empty when no code column is found.
    """
    if df is None or len(df) == 0:
        return []

    columns = list(df.columns)

    # Preferred: an explicit constituent-code column ("成分券代码").
    preferred = [
        c for c in columns if not _is_index_code_column(c) and "代码" in str(c) and ("成分" in str(c) or "券" in str(c))
    ]
    # Fallback: any code-like column that is not the index's own code.
    candidates = preferred or [c for c in columns if not _is_index_code_column(c)]

    for column in candidates:
        series = df[column].astype(str).str.strip()
        usable = series[series.str.fullmatch(r"\d{6}")]
        # A real code column is entirely six-digit; mixed content means dates/names.
        if usable.empty or len(usable) != len(series):
            continue

        raw_codes = usable.tolist()
        filtered = [c for c in raw_codes if c != index_digits and not c.startswith("399")]
        if not filtered:
            # Everything looked like an index code: wrong column, keep looking.
            continue

        parsed: list[str] = []
        unrecognised: list[str] = []
        for code in filtered:
            try:
                parsed.append(normalize(code))
            except DataError:
                # A single newly-issued prefix must not abort the whole sync.
                unrecognised.append(code)
        if unrecognised:
            log.warning(
                "index %s: skipped %d unrecognised constituent code(s): %s",
                index_digits,
                len(unrecognised),
                unrecognised[:10],
            )
        if parsed:
            return parsed

    return []
