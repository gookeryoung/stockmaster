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

export interface SchedulerJob {
  id: string;
  /** 中文任务名（后端 JOB_META 提供） */
  label: string;
  /** 中文说明 */
  description: string;
  /** 人类可读的执行计划，如「交易日 15:20」 */
  schedule: string;
  next_run: string | null;
  trigger?: string;
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
  scheduler: { running: boolean; timezone?: string; jobs?: SchedulerJob[] };
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

// ---------------------------------------------------------------------------
// Accounts / authentication
// ---------------------------------------------------------------------------

export type UserRole = "admin" | "user";
export type UserStatus = "active" | "disabled";

export interface Identity {
  id: number;
  provider: string;
  nickname: string;
  avatar_url: string;
  created_at: string | null;
}

export interface AuthUser {
  id: number;
  username: string;
  email: string | null;
  nickname: string;
  avatar_url: string;
  role: UserRole;
  status: UserStatus;
  last_login_at: string | null;
  created_at: string | null;
  has_password: boolean;
  identities: Identity[];
}

export interface AuthConfig {
  auth_enabled: boolean;
  allow_register: boolean;
  wechat_enabled: boolean;
  wechat_mode: string;
  has_users: boolean;
  /** 数据是否按工作区隔离；关闭时所有人共享一份数据，不显示工作区入口。 */
  workspace_enabled: boolean;
  version: string;
}

export type WorkspaceRole = "owner" | "editor" | "viewer";

export interface Workspace {
  id: number;
  name: string;
  description: string;
  kind: "personal" | "shared";
  owner_id: number | null;
  /** 当前账号在该工作区的角色（不是工作区的「主所有者」）。 */
  role: WorkspaceRole;
  member_count: number;
  created_at: string | null;
  updated_at: string | null;
}

export interface WorkspaceMember {
  user_id: number;
  username: string;
  nickname: string;
  avatar_url: string;
  role: WorkspaceRole;
  joined_at: string | null;
}

export interface TokenResponse {
  token: string;
  token_type: string;
  expires_at: string;
  user: AuthUser;
}

export interface UserPage {
  total: number;
  items: AuthUser[];
}

export interface AuditLog {
  id: number;
  user_id: number | null;
  username: string;
  action: string;
  detail: string;
  ip: string;
  created_at: string | null;
}

// ---------------------------------------------------------------------------
// System backup / restore (admin)
// ---------------------------------------------------------------------------

export interface BackupEntry {
  name: string;
  size: number;
  created_at: string;
  app_version?: string;
  note?: string;
  schema_version?: string;
  tables?: number;
  rows?: number;
  has_models?: boolean;
  has_secret?: boolean;
  error?: string;
}

export interface BackupList {
  directory: string;
  keep: number;
  total_size: number;
  items: BackupEntry[];
}

export interface BackupCreated {
  name: string;
  size: number;
  path: string;
  warnings: string[];
}

export interface RestoreInspection {
  manifest: {
    version: string;
    app_version: string;
    created_at: string;
    note?: string;
    database?: { schema_version?: string; tables?: string[]; row_counts?: Record<string, number> };
  };
  summary: string;
  archive_size: number;
  known_schema: boolean;
  backup_ahead: boolean;
  target: { tables: number; rows: number; schema_version: string };
}
