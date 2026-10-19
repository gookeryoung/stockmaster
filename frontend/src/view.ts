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

interface ViewState {
  market: MarketView;
  analyze: AnalyzeView;
  predict: PredictView;
  backtest: BacktestView;
  alerts: AlertsView;
  settings: SettingsView;

  setMarket: (patch: Partial<MarketView>) => void;
  setAnalyze: (patch: Partial<AnalyzeView>) => void;
  setPredict: (patch: Partial<PredictView>) => void;
  setBacktest: (patch: Partial<BacktestView>) => void;
  setAlerts: (patch: Partial<Omit<AlertsView, "form">>) => void;
  setAlertForm: (patch: Partial<AlertRuleForm>) => void;
  setSettings: (patch: Partial<SettingsView>) => void;
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

      setMarket: (patch) => set((state) => ({ market: { ...state.market, ...patch } })),
      setAnalyze: (patch) => set((state) => ({ analyze: { ...state.analyze, ...patch } })),
      setPredict: (patch) => set((state) => ({ predict: { ...state.predict, ...patch } })),
      setBacktest: (patch) => set((state) => ({ backtest: { ...state.backtest, ...patch } })),
      setAlerts: (patch) => set((state) => ({ alerts: { ...state.alerts, ...patch } })),
      setAlertForm: (patch) =>
        set((state) => ({ alerts: { ...state.alerts, form: { ...state.alerts.form, ...patch } } })),
      setSettings: (patch) => set((state) => ({ settings: { ...state.settings, ...patch } })),
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
      }),
    },
  ),
);
