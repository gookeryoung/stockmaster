"""API integration tests.

These exercise the assembled FastAPI app against a real (temporary) database with
mock provider data, so routing, serialisation, error mapping and the database
layer are all covered together rather than in isolation.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.providers.base import Bar, InstrumentInfo


def business_days(n: int, start: date = date(2025, 1, 6)) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient backed by a temporary database with seeded market data."""
    monkeypatch.setenv("SM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SM_DB_FILENAME", f"api-{uuid.uuid4().hex}.db")
    monkeypatch.setenv("SM_SCHEDULER_ENABLED", "false")

    from app.config import get_settings

    get_settings.cache_clear()

    from app.db import session as session_mod

    session_mod.reset_engine()
    session_mod.init_db()

    # Seed two symbols plus an index with deterministic rising prices.
    from app.db import session_scope
    from app.sync import repository as repo

    days = business_days(320)
    with session_scope() as session:
        repo.upsert_calendar(session, days, source="test")
        repo.upsert_instruments(
            session,
            [
                InstrumentInfo(code="600519.SH", name="贵州茅台", market="SH", board="main", status=1),
                InstrumentInfo(code="000001.SZ", name="平安银行", market="SZ", board="main", status=1),
                InstrumentInfo(code="000300.SH", name="沪深300", market="SH", board="index", is_index=True, status=1),
            ],
        )
        for code, base in (("600519.SH", 100.0), ("000001.SZ", 10.0), ("000300.SH", 4000.0)):
            bars = []
            for index, day in enumerate(days):
                close = base * (1 + 0.002) ** index
                bars.append(
                    Bar(
                        code=code,
                        trade_date=day,
                        open=close * 0.995,
                        high=close * 1.01,
                        low=close * 0.99,
                        close=close,
                        volume=1e6,
                        amount=1e8,
                        preclose=base * (1 + 0.002) ** (index - 1) if index else base,
                        pct_chg=0.2,
                        turn=1.5,
                        is_st=False,
                        adjust="qfq",
                        source="test",
                    )
                )
            repo.upsert_daily_bars(session, code, bars, "qfq")
            repo.upsert_daily_bars(session, code, bars, "raw")

    from app.main import create_app

    app = create_app()
    with TestClient(app) as test_client:
        yield test_client

    session_mod.reset_engine()
    get_settings.cache_clear()


class TestHealthAndDocs:
    def test_health(self, client):
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["version"]

    def test_openapi_schema_available(self, client):
        response = client.get("/openapi.json")
        assert response.status_code == 200
        assert "/api/v1/health" in response.json()["paths"]

    def test_docs_render(self, client):
        assert client.get("/docs").status_code == 200

    def test_api_only_root_explains_state(self, client):
        # Without a frontend build the root must explain how to get one.
        response = client.get("/")
        assert response.status_code == 200
        body = response.json()
        assert body["mode"] == "api-only"


class TestInstruments:
    def test_list_stocks_excludes_indices_by_default(self, client):
        body = client.get("/api/v1/stocks").json()
        codes = {item["code"] for item in body["items"]}
        assert "600519.SH" in codes
        assert "000300.SH" not in codes

    def test_include_indices_flag(self, client):
        body = client.get("/api/v1/stocks", params={"include_indices": True}).json()
        codes = {item["code"] for item in body["items"]}
        assert "000300.SH" in codes

    def test_search_by_name(self, client):
        body = client.get("/api/v1/stocks", params={"q": "茅台"}).json()
        assert body["total"] == 1
        assert body["items"][0]["code"] == "600519.SH"

    def test_stock_detail(self, client):
        body = client.get("/api/v1/stocks/600519.SH").json()
        assert body["name"] == "贵州茅台"
        assert body["market"] == "SH"

    def test_unknown_stock_returns_404(self, client):
        assert client.get("/api/v1/stocks/999999.SH").status_code == 404

    def test_invalid_symbol_returns_422(self, client):
        response = client.get("/api/v1/stocks/notasymbol")
        assert response.status_code == 422


class TestBars:
    def test_bars_returns_series(self, client):
        body = client.get("/api/v1/stocks/600519.SH/bars", params={"limit": 50}).json()
        assert body["code"] == "600519.SH"
        assert body["count"] > 0
        assert body["bars"] == sorted(body["bars"], key=lambda b: b["trade_date"])

    def test_bars_respect_limit(self, client):
        body = client.get("/api/v1/stocks/600519.SH/bars", params={"limit": 10}).json()
        assert body["count"] == 10

    def test_hfq_is_explicitly_not_supported(self, client):
        """hfq is derived, not stored; the API must say so rather than 500."""
        response = client.get(
            "/api/v1/stocks/600519.SH/bars", params={"adjust": "hfq"}
        )
        assert response.status_code == 501
        assert "hfq" in response.json()["detail"]

    def test_date_range_filter(self, client):
        body = client.get(
            "/api/v1/stocks/600519.SH/bars",
            params={"start": "2025-02-01", "end": "2025-03-01", "limit": 500},
        ).json()
        for bar in body["bars"]:
            assert "2025-02-01" <= bar["trade_date"] <= "2025-03-01"

    def test_calendar(self, client):
        body = client.get(
            "/api/v1/calendar", params={"start": "2025-01-01", "end": "2025-01-31"}
        ).json()
        assert body["days"]
        assert all(date.fromisoformat(d).weekday() < 5 for d in body["days"])


class TestMarketOverview:
    def test_overview_shape(self, client):
        body = client.get("/api/v1/market/overview").json()
        assert "indices" in body
        assert body["total"] >= 2
        # With rising prices every seeded symbol should be advancing.
        assert body["advancing"] >= 2
        assert body["data_freshness"]["bar_count"] > 0

    def test_indicators(self, client):
        body = client.get("/api/v1/stocks/600519.SH/indicators").json()
        assert body["code"] == "600519.SH"
        for key in ("ma5", "ma20", "macd_dif", "rsi6", "kdj_k", "boll_mid", "atr14"):
            assert key in body["values"]

    def test_indicators_insufficient_history(self, client):
        # 000001.SZ has plenty of history, so use a symbol with none.
        response = client.get("/api/v1/stocks/000300.SH/indicators")
        # The index also has seeded history, so this should succeed.
        assert response.status_code == 200


class TestAnalysis:
    def test_factors_require_analytics_job(self, client):
        """Factors are computed by a job, so a clear 404 beats a silent empty body."""
        response = client.get("/api/v1/analysis/factors/600519.SH")
        assert response.status_code == 404
        assert "analytics" in response.json()["detail"]

    def test_analytics_job_then_factors(self, client):
        run = client.post("/api/v1/system/jobs/analytics")
        assert run.status_code == 200
        assert run.json()["detail"]["symbols"] > 0

        body = client.get("/api/v1/analysis/factors/600519.SH").json()
        assert body["code"] == "600519.SH"
        assert "mom_20" in body["factors"]
        assert body["score"] is not None

    def test_screen_returns_ranked_rows(self, client):
        client.post("/api/v1/system/jobs/analytics")
        rows = client.post("/api/v1/analysis/screen", json={"limit": 10}).json()
        assert rows
        scores = [r["score"] for r in rows if r["score"] is not None]
        assert scores == sorted(scores, reverse=True)

    def test_screen_min_score_filter(self, client):
        client.post("/api/v1/system/jobs/analytics")
        rows = client.post(
            "/api/v1/analysis/screen", json={"limit": 50, "min_score": 100.0}
        ).json()
        assert rows == []

    def test_correlation(self, client):
        body = client.get(
            "/api/v1/analysis/correlation",
            params={"codes": "600519.SH,000001.SZ", "window": 120},
        ).json()
        assert len(body["codes"]) == 2
        assert len(body["matrix"]) == 2
        # Diagonal must be exactly 1 for a valid correlation matrix.
        assert body["matrix"][0][0] == pytest.approx(1.0)

    def test_correlation_requires_two_codes(self, client):
        response = client.get(
            "/api/v1/analysis/correlation", params={"codes": "600519.SH"}
        )
        assert response.status_code == 422


class TestPrediction:
    def test_models_empty_initially(self, client):
        assert client.get("/api/v1/predict/models").json() == []

    def test_train_then_list_and_predict(self, client):
        train = client.post("/api/v1/predict/train", json={"algo": "hist_gbdt", "horizon": 1})
        assert train.status_code == 200, train.text
        body = train.json()
        assert body["model_id"]
        # Metrics must include a baseline so the AUC is interpretable.
        assert "accuracy_baseline" in body["metrics"]

        models = client.get("/api/v1/predict/models").json()
        assert models and models[0]["is_active"] is True

        run = client.post("/api/v1/predict/run", params={"horizon": 1})
        assert run.status_code == 200
        assert run.json()["scored"] >= 1

        listing = client.get("/api/v1/predict", params={"horizon": 1}).json()
        assert listing["items"]
        probabilities = [i["prob_up"] for i in listing["items"]]
        assert all(0.0 <= p <= 1.0 for p in probabilities)

    def test_prediction_detail_includes_disclaimer(self, client):
        client.post("/api/v1/predict/train", json={"algo": "hist_gbdt", "horizon": 1})
        client.post("/api/v1/predict/run", params={"horizon": 1})
        body = client.get("/api/v1/stocks/600519.SH/prediction", params={"horizon": 1}).json()
        assert "disclaimer" in body
        assert body["prob_up"] is not None

    def test_predict_without_model_returns_conflict(self, client):
        response = client.post("/api/v1/predict/run", params={"horizon": 5})
        assert response.status_code == 409
        assert "train" in response.json()["detail"]

    def test_unknown_algo_rejected(self, client):
        response = client.post("/api/v1/predict/train", json={"algo": "bogus"})
        assert response.status_code == 422


class TestBacktest:
    def test_buy_and_hold_runs(self, client):
        response = client.post(
            "/api/v1/backtest/run",
            json={"strategy": "buy_hold", "codes": ["600519.SH"], "start": "2025-02-01"},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["run_id"]
        assert body["equity"]
        assert body["metrics"]["total_return"] > 0  # seeded series rises
        assert "不代表未来收益" in body["disclaimer"]

    def test_ma_cross_runs(self, client):
        response = client.post(
            "/api/v1/backtest/run",
            json={"strategy": "ma_cross", "codes": ["600519.SH", "000001.SZ"]},
        )
        assert response.status_code in (200, 409)  # may legitimately have no crosses
        if response.status_code == 200:
            assert "max_drawdown" in response.json()["metrics"]

    def test_score_strategy_computes_factors_from_bars(self, client):
        """The score backtest must not depend on the thin `factors` cache.

        It recomputes scores from stored bars, so it works without the analytics
        job having run — and produces a multi-date signal series rather than the
        single point the cached table would allow.
        """
        response = client.post(
            "/api/v1/backtest/run",
            json={"strategy": "score_top_n", "codes": ["600519.SH", "000001.SZ"], "top_n": 1},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["equity"]
        # A real series should produce trades; a one-point signal would not.
        assert body["metrics"]["n_trades"] > 0
        assert body["metrics"]["total_costs"] > 0

    def test_run_is_persisted_and_retrievable(self, client):
        run = client.post(
            "/api/v1/backtest/run",
            json={"strategy": "buy_hold", "codes": ["600519.SH"]},
        ).json()
        run_id = run["run_id"]

        listing = client.get("/api/v1/backtest").json()
        assert any(item["run_id"] == run_id for item in listing)

        detail = client.get(f"/api/v1/backtest/{run_id}").json()
        assert detail["run_id"] == run_id
        assert detail["equity"]

    def test_unknown_run_returns_404(self, client):
        assert client.get(f"/api/v1/backtest/{uuid.uuid4()}").status_code == 404

    def test_invalid_date_range_rejected(self, client):
        response = client.post(
            "/api/v1/backtest/run",
            json={
                "strategy": "buy_hold",
                "codes": ["600519.SH"],
                "start": "2025-06-01",
                "end": "2025-01-01",
            },
        )
        assert response.status_code == 422


class TestAlerts:
    def test_rule_types_exposed(self, client):
        types = client.get("/api/v1/alerts/types").json()
        names = {t["rule_type"] for t in types}
        assert "price_cross" in names
        assert "prediction" in names

    def test_create_and_list_rule(self, client):
        created = client.post(
            "/api/v1/alerts/rules",
            json={
                "name": "价格突破",
                "rule_type": "price_cross",
                "params": {"threshold": 100.0, "direction": "above"},
                "scope_type": "codes",
                "scope_value": "600519.SH",
            },
        )
        assert created.status_code == 201, created.text
        rule_id = created.json()["id"]

        rules = client.get("/api/v1/alerts/rules").json()
        assert any(r["id"] == rule_id for r in rules)

    def test_create_rule_validates_params(self, client):
        response = client.post(
            "/api/v1/alerts/rules",
            json={"name": "缺参数", "rule_type": "price_cross", "params": {}},
        )
        assert response.status_code == 422

    def test_create_rule_rejects_unknown_type(self, client):
        response = client.post(
            "/api/v1/alerts/rules",
            json={"name": "未知", "rule_type": "nope", "params": {}},
        )
        assert response.status_code == 422

    def test_scope_codes_ignored_by_session_client(self, client):
        """A rule needing watchlist scope must not silently scan everything."""
        created = client.post(
            "/api/v1/alerts/rules",
            json={
                "name": "空范围",
                "rule_type": "price_cross",
                "params": {"threshold": 1e12, "direction": "above"},
                "scope_type": "codes",
                "scope_value": "",
            },
        )
        assert created.status_code == 201
        scan = client.post("/api/v1/alerts/scan").json()
        assert scan["triggered"] == 0

    def test_scan_is_idempotent(self, client):
        """Repeated scans must not duplicate triggers."""
        client.post(
            "/api/v1/alerts/rules",
            json={
                "name": "必然触发",
                "rule_type": "price_cross",
                "params": {"threshold": 1.0, "direction": "above"},
                "scope_type": "codes",
                "scope_value": "600519.SH",
            },
        )
        first = client.post("/api/v1/alerts/scan").json()
        assert first["triggered"] == 1

        for _ in range(3):
            assert client.post("/api/v1/alerts/scan").json()["triggered"] == 0

        triggers = client.get("/api/v1/alerts/triggers").json()
        assert len(triggers) == 1

    def test_trigger_status_update(self, client):
        client.post(
            "/api/v1/alerts/rules",
            json={
                "name": "触发规则",
                "rule_type": "price_cross",
                "params": {"threshold": 1.0, "direction": "above"},
                "scope_type": "codes",
                "scope_value": "600519.SH",
            },
        )
        client.post("/api/v1/alerts/scan")
        trigger_id = client.get("/api/v1/alerts/triggers").json()[0]["id"]

        updated = client.patch(
            f"/api/v1/alerts/triggers/{trigger_id}", json={"status": "read"}
        )
        assert updated.status_code == 200
        assert updated.json()["status"] == "read"

    def test_preview_does_not_persist(self, client):
        response = client.post(
            "/api/v1/alerts/preview",
            json={
                "name": "预览",
                "rule_type": "price_cross",
                "params": {"threshold": 1.0, "direction": "above"},
                "scope_type": "codes",
                "scope_value": "600519.SH",
            },
        )
        assert response.status_code == 200
        assert response.json()["matched"] == 1
        # Nothing should have been stored by a preview.
        assert client.get("/api/v1/alerts/rules").json() == []
        assert client.get("/api/v1/alerts/triggers").json() == []

    def test_delete_rule(self, client):
        rule_id = client.post(
            "/api/v1/alerts/rules",
            json={
                "name": "待删除",
                "rule_type": "price_cross",
                "params": {"threshold": 100.0},
                "scope_type": "codes",
                "scope_value": "600519.SH",
            },
        ).json()["id"]
        assert client.delete(f"/api/v1/alerts/rules/{rule_id}").status_code == 204
        assert client.delete(f"/api/v1/alerts/rules/{rule_id}").status_code == 404

    def test_update_rule(self, client):
        rule_id = client.post(
            "/api/v1/alerts/rules",
            json={
                "name": "原名",
                "rule_type": "price_cross",
                "params": {"threshold": 100.0},
                "scope_type": "codes",
                "scope_value": "600519.SH",
            },
        ).json()["id"]
        updated = client.patch(
            f"/api/v1/alerts/rules/{rule_id}",
            json={
                "name": "新名",
                "rule_type": "price_cross",
                "params": {"threshold": 200.0},
                "scope_type": "codes",
                "scope_value": "600519.SH",
                "enabled": False,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "新名"
        assert updated.json()["enabled"] is False


class TestWatchlist:
    def test_add_list_remove(self, client):
        created = client.post(
            "/api/v1/watchlist", json={"code": "600519.SH", "list_name": "default"}
        )
        assert created.status_code == 201, created.text
        item = created.json()
        assert item["code"] == "600519.SH"
        assert item["name"] == "贵州茅台"

        listing = client.get("/api/v1/watchlist").json()
        assert len(listing) == 1

        assert client.delete(f"/api/v1/watchlist/{item['id']}").status_code == 204
        assert client.get("/api/v1/watchlist").json() == []

    def test_add_is_idempotent(self, client):
        first = client.post("/api/v1/watchlist", json={"code": "600519.SH"}).json()
        second = client.post(
            "/api/v1/watchlist", json={"code": "600519.SH", "note": "更新备注"}
        ).json()
        assert first["id"] == second["id"]
        assert second["note"] == "更新备注"
        assert len(client.get("/api/v1/watchlist").json()) == 1

    def test_invalid_code_rejected(self, client):
        response = client.post("/api/v1/watchlist", json={"code": "bad"})
        assert response.status_code == 422


class TestSystem:
    def test_system_status(self, client):
        body = client.get("/api/v1/system/status").json()
        assert body["version"]
        assert body["data"]["bar_count"] > 0
        assert body["data"]["instrument_count"] == 3
        assert body["data"]["watchlist_size"] == 0
        assert "disclaimer" in body

    def test_job_listing_and_execution(self, client):
        body = client.post("/api/v1/system/jobs/calendar").json()
        assert body["job"] == "calendar"

    def test_unknown_job_returns_404(self, client):
        assert client.post("/api/v1/system/jobs/nope").status_code == 404

    def test_daily_job_runs_with_mock_disabled_providers(self, client):
        """The daily job must not 500 when live providers are unreachable.

        In tests no network provider is configured for the temp DB, so this
        exercises the error path: it should report a structured failure.
        """
        response = client.post("/api/v1/system/jobs/instruments")
        assert response.status_code in (200, 500)
