/** 各视图的操作状态（表单草稿、筛选条件、选中项）。
 *
 * 路由切换会卸载页面组件，组件内的 useState 随之丢失——离开再回来就得把代码、
 * 日期、筛选条件重填一遍。这里把这些「操作状态」集中到一个带 persist 的 zustand
 * store：来回切换视图、甚至刷新页面后都能原样恢复。
 *
 * 边界：只放操作状态，不放服务端数据（行情、回测结果、预测列表等交给 React Query
 * 缓存），避免把可再生数据混进本地存储导致陈旧。
 */

import { create } from "zustand";
import { persist } from "zustand/middleware";
import { todayISO, yearsAgoISO } from "./format";
import type { SampleUniverseType } from "./types";

export interface MarketView {
  search: string;
  selected: string;
  adjust: string;
}

export interface AnalyzeView {
  code: string;
  correlationInput: string;
  screenMinScore: string;
}

export interface PredictView {
  algo: string;
  horizon: number;
  detailCode: string;
}

export interface BacktestView {
  strategy: string;
  codes: string;
  start: string;
  end: string;
  cash: number;
  topN: number;
  rebalanceDays: number;
  commission: number;
  slippage: number;
}

export interface AlertRuleForm {
  name: string;
  rule_type: string;
  scope_type: string;
  scope_value: string;
  once_per_day: boolean;
  exclude_st: boolean;
  min_amount: number;
}

export interface AlertsView {
  statusFilter: string;
  form: AlertRuleForm;
  params: Record<string, string>;
}

export interface SettingsView {
  newWatchCode: string;
}

/** 策略配置页的三个 tab 与各自的表单草稿。
 *
 * ``conditions`` 用纯文本维护（每行 ``field op value``），提交前解析成
 * ``ScreeningCondition[]``：用 JSON 文本框对普通用户太不友好，而结构化条件行
 * 编辑器在表单里会占掉半屏。``params`` 同理保存超参的字符串草稿。
 */
export interface ScreeningConfigForm {
  name: string;
  description: string;
  min_score: string;
  max_vol: string;
  min_amount_log: string;
  exclude_st: boolean;
  boards: string;
  conditions: string;
  order_by: string;
  direction: "asc" | "desc";
  limit: number;
}

export interface SampleConfigForm {
  name: string;
  description: string;
  universe_type: SampleUniverseType;
  universe_value: string;
  start: string;
  end: string;
  lookback_days: number;
  horizon: number;
  min_bars: number;
}

export interface AlgorithmConfigForm {
  name: string;
  description: string;
  algo: string;
  sample_id: string;
  horizon: number;
  test_fraction: string;
  params: Record<string, string>;
}

export type StrategyTab = "screening" | "sample" | "algorithm";

export interface StrategyView {
  tab: StrategyTab;
  screening: ScreeningConfigForm;
  sample: SampleConfigForm;
  algorithm: AlgorithmConfigForm;
}

interface ViewState {
  market: MarketView;
  analyze: AnalyzeView;
  predict: PredictView;
  backtest: BacktestView;
  alerts: AlertsView;
  settings: SettingsView;
  strategy: StrategyView;

  setMarket: (patch: Partial<MarketView>) => void;
  setAnalyze: (patch: Partial<AnalyzeView>) => void;
  setPredict: (patch: Partial<PredictView>) => void;
  setBacktest: (patch: Partial<BacktestView>) => void;
  setAlerts: (patch: Partial<Omit<AlertsView, "form">>) => void;
  setAlertForm: (patch: Partial<AlertRuleForm>) => void;
  setSettings: (patch: Partial<SettingsView>) => void;
  setStrategy: (patch: Partial<Omit<StrategyView, "screening" | "sample" | "algorithm">>) => void;
  setScreeningForm: (patch: Partial<ScreeningConfigForm>) => void;
  setSampleForm: (patch: Partial<SampleConfigForm>) => void;
  setAlgorithmForm: (patch: Partial<AlgorithmConfigForm>) => void;
}

const DEFAULT_MARKET: MarketView = { search: "", selected: "600519.SH", adjust: "qfq" };
const DEFAULT_ANALYZE: AnalyzeView = {
  code: "600519.SH",
  correlationInput: "600519.SH,000001.SZ,300750.SZ",
  screenMinScore: "",
};
const DEFAULT_PREDICT: PredictView = { algo: "hist_gbdt", horizon: 1, detailCode: "" };
const DEFAULT_BACKTEST: BacktestView = {
  strategy: "buy_hold",
  codes: "600519.SH",
  start: yearsAgoISO(2),
  end: todayISO(),
  cash: 1_000_000,
  topN: 10,
  rebalanceDays: 5,
  commission: 0.00025,
  slippage: 0.0005,
};
const DEFAULT_ALERTS: AlertsView = {
  statusFilter: "",
  form: {
    name: "新预警规则",
    rule_type: "price_cross",
    scope_type: "watchlist",
    scope_value: "default",
    once_per_day: true,
    exclude_st: true,
    min_amount: 0,
  },
  params: { threshold: "100", direction: "above" },
};
const DEFAULT_SETTINGS: SettingsView = { newWatchCode: "" };
const DEFAULT_STRATEGY: StrategyView = {
  tab: "screening",
  screening: {
    name: "",
    description: "",
    min_score: "60",
    max_vol: "",
    min_amount_log: "",
    exclude_st: true,
    boards: "",
    conditions: "",
    order_by: "score",
    direction: "desc",
    limit: 50,
  },
  sample: {
    name: "",
    description: "",
    universe_type: "watchlist",
    universe_value: "default",
    start: "",
    end: "",
    lookback_days: 730,
    horizon: 5,
    min_bars: 60,
  },
  algorithm: {
    name: "",
    description: "",
    algo: "hist_gbdt",
    sample_id: "",
    horizon: 5,
    test_fraction: "0.2",
    params: { max_iter: "300", learning_rate: "0.05", max_depth: "4", min_samples_leaf: "50" },
  },
};

export const useViewState = create<ViewState>()(
  persist(
    (set) => ({
      market: { ...DEFAULT_MARKET },
      analyze: { ...DEFAULT_ANALYZE },
      predict: { ...DEFAULT_PREDICT },
      backtest: { ...DEFAULT_BACKTEST },
      alerts: {
        ...DEFAULT_ALERTS,
        form: { ...DEFAULT_ALERTS.form },
        params: { ...DEFAULT_ALERTS.params },
      },
      settings: { ...DEFAULT_SETTINGS },
      strategy: {
        ...DEFAULT_STRATEGY,
        screening: { ...DEFAULT_STRATEGY.screening },
        sample: { ...DEFAULT_STRATEGY.sample },
        algorithm: { ...DEFAULT_STRATEGY.algorithm, params: { ...DEFAULT_STRATEGY.algorithm.params } },
      },

      setMarket: (patch) => set((state) => ({ market: { ...state.market, ...patch } })),
      setAnalyze: (patch) => set((state) => ({ analyze: { ...state.analyze, ...patch } })),
      setPredict: (patch) => set((state) => ({ predict: { ...state.predict, ...patch } })),
      setBacktest: (patch) => set((state) => ({ backtest: { ...state.backtest, ...patch } })),
      setAlerts: (patch) => set((state) => ({ alerts: { ...state.alerts, ...patch } })),
      setAlertForm: (patch) =>
        set((state) => ({ alerts: { ...state.alerts, form: { ...state.alerts.form, ...patch } } })),
      setSettings: (patch) => set((state) => ({ settings: { ...state.settings, ...patch } })),
      setStrategy: (patch) => set((state) => ({ strategy: { ...state.strategy, ...patch } })),
      setScreeningForm: (patch) =>
        set((state) => ({ strategy: { ...state.strategy, screening: { ...state.strategy.screening, ...patch } } })),
      setSampleForm: (patch) =>
        set((state) => ({ strategy: { ...state.strategy, sample: { ...state.strategy.sample, ...patch } } })),
      setAlgorithmForm: (patch) =>
        set((state) => ({
          strategy: { ...state.strategy, algorithm: { ...state.strategy.algorithm, ...patch } },
        })),
    }),
    {
      name: "stockmaster.view",
      version: 1,
      partialize: (state) => ({
        market: state.market,
        analyze: state.analyze,
        predict: state.predict,
        backtest: state.backtest,
        alerts: state.alerts,
        settings: state.settings,
        strategy: state.strategy,
      }),
    },
  ),
);
