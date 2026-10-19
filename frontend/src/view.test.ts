import { beforeEach, describe, expect, it, vi } from "vitest";

const PERSIST_KEY = "stockmaster.view";

async function load() {
  return import("./view");
}

beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
});

describe("默认视图状态", () => {
  it("首屏给出可用的示例代码与回测区间", async () => {
    const { useViewState } = await load();
    const state = useViewState.getState();

    expect(state.market).toEqual({ search: "", selected: "600519.SH", adjust: "qfq" });
    expect(state.analyze.code).toBe("600519.SH");
    expect(state.backtest.strategy).toBe("buy_hold");
    expect(state.backtest.start < state.backtest.end).toBe(true);
  });
});

describe("状态更新", () => {
  it("setMarket 做浅合并，未提及的字段保持原值", async () => {
    const { useViewState } = await load();

    useViewState.getState().setMarket({ search: "招商" });
    useViewState.getState().setMarket({ adjust: "raw" });

    expect(useViewState.getState().market).toEqual({
      search: "招商",
      selected: "600519.SH",
      adjust: "raw",
    });
  });

  it("setAlertForm 只替换表单字段，不动筛选条件", async () => {
    const { useViewState } = await load();
    useViewState.getState().setAlerts({ statusFilter: "triggered" });
    useViewState.getState().setAlertForm({ min_amount: 5000 });

    const alerts = useViewState.getState().alerts;
    expect(alerts.statusFilter).toBe("triggered");
    expect(alerts.form.min_amount).toBe(5000);
    expect(alerts.form.rule_type).toBe("price_cross");
  });

  it("setBacktest 只覆盖传入字段", async () => {
    const { useViewState } = await load();
    const before = useViewState.getState().backtest;

    useViewState.getState().setBacktest({ cash: 500_000 });

    expect(useViewState.getState().backtest.cash).toBe(500_000);
    expect(useViewState.getState().backtest.topN).toBe(before.topN);
  });
});

describe("持久化", () => {
  it("状态写入 localStorage，便于刷新后恢复", async () => {
    const { useViewState } = await load();
    useViewState.getState().setMarket({ selected: "000001.SZ" });

    const raw = localStorage.getItem(PERSIST_KEY);
    expect(raw).toBeTruthy();
    const persisted = JSON.parse(raw as string);
    expect(persisted.state.market.selected).toBe("000001.SZ");
    expect(persisted.version).toBe(1);
  });

  it("重新加载后从 localStorage 恢复", async () => {
    localStorage.setItem(
      PERSIST_KEY,
      JSON.stringify({
        version: 1,
        state: { market: { search: "茅台", selected: "600519.SH", adjust: "hfq" } },
      }),
    );

    const { useViewState } = await load();
    // zustand persist 在模块初始化时同步 rehydrate
    expect(useViewState.getState().market).toMatchObject({ search: "茅台", adjust: "hfq" });
    // 未被持久化的分支回落到默认值，不会变成 undefined
    expect(useViewState.getState().predict.algo).toBe("hist_gbdt");
  });

  it("损坏的持久化数据不导致崩溃", async () => {
    localStorage.setItem(PERSIST_KEY, "not json");
    const { useViewState } = await load();
    expect(useViewState.getState().market.selected).toBe("600519.SH");
  });
});
