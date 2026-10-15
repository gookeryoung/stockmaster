"""A-share symbol normalisation.

The codebase uses one canonical symbol form internally:

    ``"600519.SH"``  — 6 digits, dot, upper-case exchange

Providers each want a different form, so conversion lives here:

* baostock   -> ``sh.600519``
* tencent    -> ``sh600519``
* sina       -> ``sh600519``
* eastmoney  -> ``1.600519`` (secid; retained for completeness)
"""

from __future__ import annotations

import re

from app.core.errors import DataError

_SH_RE = re.compile(r"^(600|601|603|605|688|689|900)\d{3}$")
_SZ_RE = re.compile(r"^(000|001|002|003|300|301|200)\d{3}$")
_BJ_RE = re.compile(r"^(43|83|87|88|920)\d{3,}$")

EXCHANGE_CN = {"SH": "上海", "SZ": "深圳", "BJ": "北京"}


def infer_exchange(code: str) -> str:
    """Infer the exchange for a bare 6-digit A-share code.

    Raises:
        DataError: if the code matches no known A-share/B-share prefix.
    """
    digits = re.sub(r"\D", "", code)
    if len(digits) != 6:
        raise DataError(f"cannot infer exchange for non-6-digit code: {code!r}")
    if _SH_RE.match(digits):
        return "SH"
    if _SZ_RE.match(digits):
        return "SZ"
    if _BJ_RE.match(digits):
        return "BJ"
    raise DataError(f"unrecognised A-share code prefix: {code!r}")


def normalize(code: str) -> str:
    """Return the canonical ``600519.SH`` form from any common input form.

    Accepts ``600519``, ``sh600519``, ``sh.600519``, ``600519.SH``, ``1.600519``.
    """
    raw = str(code).strip()
    if not raw:
        raise DataError("empty symbol")

    # 600519.SH / 600519.sh
    m = re.match(r"^(\d{6})[.\-_]?(sh|sz|bj)$", raw, flags=re.IGNORECASE)
    if m:
        return f"{m.group(1)}.{m.group(2).upper()}"

    # sh600519 / sh.600519 / sz.000001
    m = re.match(r"^(sh|sz|bj)[.\-_]?(\d{6})$", raw, flags=re.IGNORECASE)
    if m:
        return f"{m.group(2)}.{m.group(1).upper()}"

    # 1.600519 / 0.000001 (eastmoney secid: 1=SH, 0=SZ)
    m = re.match(r"^([01])[.](\d{6})$", raw)
    if m:
        return f"{m.group(2)}.{'SH' if m.group(1) == '1' else 'SZ'}"

    # bare digits
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 6:
        return f"{digits}.{infer_exchange(digits)}"

    raise DataError(f"cannot normalise symbol: {code!r}")


def split(code: str) -> tuple[str, str]:
    """Return ``(digits, exchange)`` for a canonical or convertible symbol."""
    canon = normalize(code)
    digits, exchange = canon.split(".")
    return digits, exchange


def to_baostock(code: str) -> str:
    """``600519.SH`` -> ``sh.600519``."""
    digits, exchange = split(code)
    return f"{exchange.lower()}.{digits}"


def to_tencent(code: str) -> str:
    """``600519.SH`` -> ``sh600519``."""
    digits, exchange = split(code)
    return f"{exchange.lower()}{digits}"


def to_sina(code: str) -> str:
    """``600519.SH`` -> ``sh600519``."""
    return to_tencent(code)


def to_eastmoney_secid(code: str) -> str:
    """``600519.SH`` -> ``1.600519``."""
    digits, exchange = split(code)
    market = {"SH": "1", "SZ": "0", "BJ": "0"}[exchange]
    return f"{market}.{digits}"


def from_baostock(raw: str) -> str:
    """``sh.600519`` -> ``600519.SH``."""
    return normalize(raw)


def is_index_digits(digits: str) -> bool:
    """Heuristic: index codes start with 000/399 for the common CSI/SZSE sets."""
    return digits.startswith(("000", "399")) and len(digits) == 6
