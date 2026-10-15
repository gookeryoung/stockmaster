/** Types mirroring the backend Pydantic schemas. */

export interface Instrument {
  code: string;
  name: string;
  market: string;
  board: string;
  is_index: boolean;
  is_st: boolean;
  status: number;
  list_date: string | null;
}

export interface Bar {
  trade_date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  preclose: number | null;
  volume: number;
  amount: number;
  turn: number | null;
  pct_chg: number | null;
  is_st: boolean;
}

export interface BarsResponse {
  code: string;
  name: string;
  adjust: string;
  count: number;
  bars: Bar[];
}

export interface IndexQuote {
  code: string;
  name: string;
  close: number | null;
  preclose: number | null;
  pct_chg: number | null;
  trade_date: string | null;
}

export interface MarketOverview {
  as_of: string | null;
  indices: IndexQuote[];
  advancing: number;
  declining: number;
  unchanged: number;
  limit_up: number;
  limit_down: number;
  total: number;
  data_freshness: Record<string, unknown>;
}

export interface IndicatorSnapshot {
  code: string;
  trade_date: string | null;
  values: Record<string, number | null>;
}

export interface FactorSnapshot {
  code: string;
  name: string;
  trade_date: string | null;
  factors: Record<string, number | null>;
  score: number | null;
  percentiles: Record<string, number | null>;
}

export interface ScreenRow {
  code: string;
  name: string;
  trade_date: string | null;
  score: number | null;
  mom_20: number | null;
  mom_60: number | null;
  vol_20: number | null;
  turnover_20: number | null;
  rsi_14: number | null;
  close: number | null;
}

export interface ModelCard {
  model_id: string;
  algo: string;
  horizon: number;
  is_active: boolean;
  trained_at: string | null;
  n_train: number;
  n_test: number;
  train_start: string | null;
  train_end: string | null;
  test_start: string | null;
  test_end: string | null;
  metrics: Record<string, unknown>;
  importance: Record<string, number>;
}

export interface PredictionOut {
  code: string;
  name: string;
  trade_date: string | null;
  horizon: number;
  prob_up: number | null;
  model_id: string;
}

export interface EquityPoint {
  trade_date: string;
  equity: number;
  cash: number;
  market_value: number;
  drawdown: number;
  benchmark: number | null;
}

export interface TradeOut {
  code: string;
  side: string;
  trade_date: string;
  price: number;
  shares: number;
  gross: number;
  commission: number;
  stamp_tax: number;
  transfer_fee: number;
  slippage: number;
  pnl: number | null;
  reason: string;
}

export interface BacktestResponse {
  run_id: string;
  strategy: string;
  metrics: Record<string, unknown>;
  warnings: string[];
  equity: EquityPoint[];
  trades: TradeOut[];
  disclaimer: string;
}

export interface AlertRule {
  id: number;
  name: string;
  rule_type: string;
  params: Record<string, unknown>;
  scope_type: string;
  scope_value: string;
  cooldown_seconds: number;
  once_per_day: boolean;
  exclude_st: boolean;
  min_amount: number;
  enabled: boolean;
  created_at: string | null;
}

export interface AlertTrigger {
  id: number;
  rule_id: number;
  code: string;
  name: string;
  trade_date: string;
  trigger_at: string | null;
  rule_type: string;
  message: string;
  snapshot: Record<string, unknown>;
  status: string;
}

export interface RuleTypeInfo {
  rule_type: string;
  description: string;
  params: Record<string, { type: string; default?: unknown; choices?: unknown[]; required?: boolean }>;
}

export interface SystemStatus {
  version: string;
  timezone: string;
  database: Record<string, unknown>;
  providers: Array<{
    name: string;
    circuit_open: boolean;
    successes: number;
    failures: number;
    served: number;
    last_error: string;
  }>;
  scheduler: { running: boolean; jobs?: Array<{ id: string; next_run: string | null }> };
  data: Record<string, unknown>;
  disclaimer: string;
}

export interface WatchlistItem {
  id: number;
  list_name: string;
  code: string;
  name: string;
  note: string;
}
