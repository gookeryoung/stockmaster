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
    expect(state.strategy.tab).toBe("screening");
    expect(state.strategy.algorithm.params.max_iter).toBe("300");
    // 预测页默认选满全部特征、带上当前算法的默认超参。
    const { ALL_FEATURES } = await import("./predictModules");
    expect(state.predict.features).toEqual(ALL_FEATURES);
    expect(state.predict.params.max_iter).toBe("300");
    expect(state.predict.testFraction).toBe("");
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

  it("setStrategy 与三个表单互不覆盖", async () => {
    const { useViewState } = await load();
    const { setStrategy, setScreeningForm, setSampleForm, setAlgorithmForm } = useViewState.getState();

    setStrategy({ tab: "algorithm" });
    setScreeningForm({ name: "高分红" });
    setSampleForm({ horizon: 20 });
    setAlgorithmForm({ algo: "logistic" });

    const strategy = useViewState.getState().strategy;
    expect(strategy.tab).toBe("algorithm");
    expect(strategy.screening.name).toBe("高分红");
    expect(strategy.sample.horizon).toBe(20);
    expect(strategy.algorithm.algo).toBe("logistic");
    // 未修改的超参必须保留
    expect(strategy.algorithm.params.learning_rate).toBe("0.05");
  });

  it("setBacktest 只覆盖传入字段", async () => {
    const { useViewState } = await load();
    const before = useViewState.getState().backtest;

    useViewState.getState().setBacktest({ cash: 500_000 });

    expect(useViewState.getState().backtest.cash).toBe(500_000);
    expect(useViewState.getState().backtest.topN).toBe(before.topN);
  });
});

describe("预测页模块化自定义草稿", () => {
  it("setPredict 浅合并：改特征子集不影响超参与周期", async () => {
    const { useViewState } = await load();

    useViewState.getState().setPredict({ features: ["ret_1", "rsi_14"] });

    const predict = useViewState.getState().predict;
    expect(predict.features).toEqual(["ret_1", "rsi_14"]);
    expect(predict.params.learning_rate).toBe("0.05");
    expect(predict.horizon).toBe(1);
  });

  it("v1 旧草稿缺失的新字段被 migrate 补成默认值", async () => {
    localStorage.setItem(
      PERSIST_KEY,
      JSON.stringify({
        version: 1,
        state: { predict: { algo: "logistic", horizon: 5, detailCode: "600519.SH" } },
      }),
    );

    const { useViewState } = await load();
    const predict = useViewState.getState().predict;
    // 已有字段保留
    expect(predict.algo).toBe("logistic");
    expect(predict.horizon).toBe(5);
    expect(predict.detailCode).toBe("600519.SH");
    // 新增字段回落默认，而不是 undefined（否则页面读取 features 会崩溃）
    expect(Array.isArray(predict.features)).toBe(true);
    expect(predict.features.length).toBeGreaterThan(0);
    expect(predict.testFraction).toBe("");
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
