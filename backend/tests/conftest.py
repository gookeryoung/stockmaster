"""Shared pytest fixtures.

The default suite is fully offline and deterministic: network-backed tests are
marked ``@pytest.mark.network`` and deselected unless explicitly requested.
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

# Make `app` importable when running pytest without an installed package.
BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.providers.base import Bar  # noqa: E402
from app.providers.mock_provider import MockProvider, synthetic_bars  # noqa: E402


def pytest_collection_modifyitems(config, items):
    """Skip ``network``-marked tests unless the user explicitly asks for them.

    A bare ``pytest`` run must stay hermetic: the real providers throttle, and
    baostock can hang, so network tests are opt-in via ``-m network``.
    """
    marker_expr = config.getoption("-m") or ""
    if "network" in marker_expr:
        return
    skip_network = pytest.mark.skip(
        reason="network test; run with `-m network` to enable"
    )
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip_network)


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the app at a throwaway SQLite file and initialise the schema."""
    from app.config import get_settings

    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", "test.db")
    get_settings.cache_clear()

    from app.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()
    yield tmp_path
    session_mod.reset_engine()
    get_settings.cache_clear()


@pytest.fixture
def mock_provider() -> MockProvider:
    return MockProvider()


@pytest.fixture
def bars_600519() -> list[Bar]:
    """Two years of synthetic weekday bars ending at a fixed date."""
    return synthetic_bars("600519.SH", date(2024, 1, 1), date(2025, 12, 31))


@pytest.fixture
def trade_days() -> list[date]:
    """A simple Mon-Fri calendar over a month, for calendar-dependent tests."""
    from datetime import timedelta

    days: list[date] = []
    day = date(2026, 1, 1)
    end = date(2026, 3, 31)
    while day <= end:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days
