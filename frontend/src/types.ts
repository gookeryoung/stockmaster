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

/** 管理员用户列表的条目：在账号信息之外附带工作区与数据量统计。
 *
 * 统计只在该接口出现（普通 `/auth/me` 等仍是 `AuthUser`），避免把管理端字段
 * 泄露到普通账号的响应里。
 */
export interface AdminUser extends AuthUser {
  /** 参与的工作区数量（含自己创建与他人共享的）。 */
  workspace_count: number;
  /** 其中自己是所有者的工作区数量。 */
  owned_workspace_count: number;
  /** 个人工作区的数据量；共享工作区不计入。 */
  personal_data_total: number;
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
  /** 工作区数据量：自选 + 预警规则 + 回测 + 三类策略配置（不含成员/通知通道）。 */
  data_total: number;
  /** 工作区级预警消息模板；空字符串 = 使用默认标题/正文。 */
  message_template: string;
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

/** 工作区通知通道：预警触发时把消息外发到 webhook / Bark / 邮件。
 *
 * `url` 由后端返回时已经脱敏：webhook/Bark 是压缩后的地址，email 是掩码收件人
 * （如 `a***@example.com`）。新建时 email 的 `url` 存放逗号分隔的收件人地址。
 */
export type WorkspaceChannelKind = "webhook" | "bark" | "email";

export interface WorkspaceChannel {
  id: number;
  name: string;
  kind: WorkspaceChannelKind;
  /** 脱敏后的地址，不能直接用于请求。 */
  url: string;
  enabled: boolean;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface WorkspaceChannelTestResult {
  delivered: boolean;
  channel: string;
  kind: string;
}

/** 工作区用量与配额。
 *
 * `usage` 与 `limits` 以资源键（members / watchlist_items / alert_rules / ...）索引；
 * `limits[key] === 0` 表示不限，由后端按 `SM_WORKSPACE_MAX_*` 配置下发。
 */
export interface WorkspaceUsage {
  workspace_id: number;
  usage: Record<string, number>;
  limits: Record<string, number>;
  labels: Record<string, string>;
  total: number;
}

/** 用量每日快照（趋势图用），后端按日期升序返回。 */
export interface WorkspaceUsagePoint {
  /** `YYYY-MM-DD` */
  captured_on: string;
  data_total: number;
  member_count: number;
  watchlist_items: number;
  alert_rules: number;
  backtest_runs: number;
  config_total: number;
}

/** 工作区预警策略的「生效值」：与规则自身设置取更严格者之后的结果。 */
export interface AlertPolicyEffective {
  exclude_st: boolean;
  min_amount: number;
  cooldown_seconds: number;
  once_per_day: boolean;
}

/** 工作区级预警策略。
 *
 * 四个可覆盖字段为 `null` 时表示「跟随规则自身设置」；`effective` 是只读的合并结果，
 * 界面展示以后者为准。策略只收紧、不放宽。
 */
export interface AlertPolicy {
  workspace_id: number;
  exclude_st: boolean | null;
  min_amount: number | null;
  cooldown_seconds: number | null;
  once_per_day: boolean | null;
  updated_at: string | null;
  effective: AlertPolicyEffective;
}

export interface TokenResponse {
  token: string;
  token_type: string;
  expires_at: string;
  user: AuthUser;
}

export interface UserPage {
  total: number;
  items: AdminUser[];
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

// ---------------------------------------------------------------------------
// Strategy configuration (workspace-scoped presets: 选股 / 样本 / 算法)
// ---------------------------------------------------------------------------

/** 选股条件的比较运算符（后端白名单同此集合）。 */
export type ScreeningOp = ">=" | "<=" | ">" | "<" | "==";

export interface ScreeningCondition {
  field: string;
  op: ScreeningOp;
  value: number;
}

export interface ScreeningParams {
  min_score: number | null;
  max_vol: number | null;
  min_amount_log: number | null;
  exclude_st: boolean;
  boards: string[];
  conditions: ScreeningCondition[];
  order_by: string;
  direction: "asc" | "desc";
  limit: number;
}

export interface ScreeningConfigInput {
  name: string;
  description?: string;
  params?: Partial<ScreeningParams>;
}

export interface ScreeningConfig {
  id: number;
  workspace_id: number;
  name: string;
  description: string;
  params: ScreeningParams;
  created_by: number | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface ScreeningRunResult {
  trade_date: string | null;
  count: number;
  items: ScreenRow[];
}

/** 样本的标的来源：自选清单 / 引用选股配置 / 显式代码 / 默认宇宙。 */
export type SampleUniverseType = "watchlist" | "screening" | "codes" | "default";

export interface SampleConfigInput {
  name: string;
  description?: string;
  universe_type: SampleUniverseType;
  universe_value?: string;
  start_date?: string | null;
  end_date?: string | null;
  lookback_days?: number | null;
  horizon?: number;
  min_bars?: number;
}

export interface SampleConfig {
  id: number;
  workspace_id: number;
  name: string;
  description: string;
  universe_type: SampleUniverseType;
  universe_value: string;
  start_date: string | null;
  end_date: string | null;
  lookback_days: number | null;
  horizon: number;
  min_bars: number;
  created_by: number | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface SampleResolveResult {
  codes: string[];
  count: number;
  start: string | null;
  end: string | null;
  horizon: number;
}

export interface AlgorithmConfigInput {
  name: string;
  description?: string;
  algo: string;
  /** 超参只接受数值：后端按算法白名单校验类型与范围。 */
  params?: Record<string, number>;
  sample_id?: number | null;
  horizon?: number;
  test_fraction?: number | null;
}

export interface AlgorithmConfig {
  id: number;
  workspace_id: number;
  name: string;
  description: string;
  algo: string;
  params: Record<string, number>;
  sample_id: number | null;
  horizon: number;
  test_fraction: number | null;
  created_by: number | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AlgorithmTrainResult {
  model_id: string;
  algo: string;
  horizon: number;
  metrics: Record<string, unknown>;
  n_train: number;
  n_test: number;
}
