/** Minimal typed API client.
 *
 * Deliberately dependency-free (fetch + a small helper) so the surface stays
 * small. Errors are converted into thrown `ApiError`s carrying the backend's
 * `detail` message, because the API returns actionable messages (for example
 * "run the analytics job first") that the UI should surface verbatim.
 */

import type {
  AlgorithmConfig,
  AlgorithmConfigInput,
  AlgorithmTrainResult,
  AlertPolicy,
  AlertRule,
  AlertTrigger,
  AuditLog,
  AuthConfig,
  AuthUser,
  BacktestResponse,
  BackupCreated,
  BackupList,
  BarsResponse,
  FactorSnapshot,
  FactorScoresResponse,
  IndicatorSnapshot,
  Instrument,
  MarketOverview,
  ModelCard,
  PredictionOut,
  QuotesResponse,
  RestoreInspection,
  RuleTypeInfo,
  SampleConfig,
  SampleConfigInput,
  SampleResolveResult,
  ScreenRow,
  ScreeningConfig,
  ScreeningConfigInput,
  ScreeningRunResult,
  SystemStatus,
  TokenResponse,
  TradeOut,
  UserPage,
  WatchlistBatchDeleteResult,
  WatchlistDeleteResult,
  WatchlistImportResult,
  WatchlistItem,
  WatchlistList,
  WatchlistRenameResult,
  Workspace,
  WorkspaceChannel,
  WorkspaceChannelKind,
  WorkspaceChannelTestResult,
  WorkspaceMember,
  WorkspaceRole,
  WorkspaceUsage,
  WorkspaceUsagePoint,
} from "./types";

export class ApiError extends Error {
  status: number;
  detail: string;
  /** 后端 `X-Auth-Error`（或中间件 body 的 `code`），用于稳定区分 403/409 的原因。 */
  code: string;
  /** 422 校验错误按字段名聚合，便于表单就地展示。 */
  fieldErrors: Record<string, string>;

  constructor(
    status: number,
    detail: string,
    options: { code?: string; fieldErrors?: Record<string, string> } = {},
  ) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.code = options.code ?? "";
    this.fieldErrors = options.fieldErrors ?? {};
  }
}

const BASE = "/api/v1";

/** Where the access token lives. localStorage (not a cookie) keeps the API
 * CSRF-free: a cross-site form cannot set an Authorization header. */
const TOKEN_KEY = "sm.auth.token";

/** Which workspace the data endpoints operate on.
 *
 * Sent as a header rather than baked into every URL: switching workspaces is then a
 * single state change instead of a site-wide link rewrite. The backend resolves the
 * caller's personal workspace when the header is absent.
 */
const WORKSPACE_KEY = "sm.workspace.id";

let workspaceId: string | null = null;
try {
  workspaceId = localStorage.getItem(WORKSPACE_KEY);
} catch {
  workspaceId = null;
}

export function getWorkspaceId(): number | null {
  const parsed = Number(workspaceId);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

export function setWorkspaceId(id: number | null): void {
  workspaceId = id && id > 0 ? String(id) : null;
  try {
    if (workspaceId) localStorage.setItem(WORKSPACE_KEY, workspaceId);
    else localStorage.removeItem(WORKSPACE_KEY);
  } catch {
    // ignore storage failures; the in-memory copy still works for this tab
  }
}

function workspaceHeader(): Record<string, string> {
  return workspaceId ? { "X-Workspace-Id": workspaceId } : {};
}

let token: string | null = null;
try {
  token = localStorage.getItem(TOKEN_KEY);
} catch {
  // Private mode / disabled storage: stay anonymous instead of crashing.
  token = null;
}

/** Fired when the server rejects the stored token, so the UI can drop the session. */
export const authEvents = new EventTarget();

export function getAuthToken(): string | null {
  return token;
}

export function setAuthToken(next: string | null): void {
  token = next;
  try {
    if (next) localStorage.setItem(TOKEN_KEY, next);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // ignore storage failures; the in-memory copy still works for this tab
  }
}

/** Build a typed error from a failed response.
 *
 * `X-Auth-Error` is the stable machine-readable discriminator (read_only,
 * quota_exceeded, ...), while `detail` stays the human message. The auth gate
 * middleware instead puts `code` in the JSON body, so both are honoured.
 */
async function buildApiError(response: Response): Promise<ApiError> {
  const headerCode = response.headers.get("X-Auth-Error") ?? "";
  let detail = `请求失败 (HTTP ${response.status})`;
  let bodyCode = "";
  const fieldErrors: Record<string, string> = {};
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") {
      detail = body.detail;
    } else if (Array.isArray(body?.detail)) {
      // FastAPI validation errors arrive as a list of objects.
      const parts = body.detail.map((item: { loc?: string[]; msg?: string }) => {
        const field = item.loc?.length ? String(item.loc[item.loc.length - 1] ?? "") : "";
        const message = String(item.msg ?? "");
        if (field) fieldErrors[field] = message;
        return `${item.loc?.slice(1).join(".") ?? ""}: ${message}`.trim();
      });
      const text = parts.join("; ");
      if (text) detail = text;
    }
    if (typeof body?.code === "string") bodyCode = body.code;
  } catch {
    // Body was not JSON (e.g. an HTML error page from a proxy); keep the generic text.
  }
  return new ApiError(response.status, detail, { code: headerCode || bodyCode, fieldErrors });
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { "Content-Type": "application/json", ...workspaceHeader() };
  if (token) headers.Authorization = `Bearer ${token}`;

  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { ...headers, ...(init?.headers as Record<string, string> | undefined) },
  });

  if (response.status === 401 && token) {
    // Expired / revoked: clear locally rather than looping on 401s.
    setAuthToken(null);
    authEvents.dispatchEvent(new Event("unauthorized"));
  }

  if (!response.ok) {
    throw await buildApiError(response);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

/** Like `request`, but returns the raw response (file downloads, binary uploads).
 *
 * Kept separate because the JSON helper sets Content-Type and parses JSON, both
 * wrong for an archive body. Error handling intentionally mirrors it so a 401
 * still drops the session instead of leaving the UI "logged in".
 */
async function requestRaw(path: string, init?: RequestInit): Promise<Response> {
  const headers: Record<string, string> = { ...workspaceHeader() };
  if (token) headers.Authorization = `Bearer ${token}`;

  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { ...headers, ...(init?.headers as Record<string, string> | undefined) },
  });

  if (response.status === 401 && token) {
    setAuthToken(null);
    authEvents.dispatchEvent(new Event("unauthorized"));
  }

  if (!response.ok) {
    throw await buildApiError(response);
  }
  return response;
}

export const api = {
  // --- auth ---
  authConfig: () => request<AuthConfig>("/auth/config"),
  login: (username: string, password: string) =>
    request<TokenResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    }),
  register: (payload: { username: string; password: string; email?: string; nickname?: string }) =>
    request<TokenResponse>("/auth/register", { method: "POST", body: JSON.stringify(payload) }),
  logout: () => request<void>("/auth/logout", { method: "POST" }),
  me: () => request<AuthUser>("/auth/me"),
  updateMe: (payload: { nickname?: string; email?: string; avatar_url?: string }) =>
    request<AuthUser>("/auth/me", { method: "PATCH", body: JSON.stringify(payload) }),
  changePassword: (oldPassword: string, newPassword: string) =>
    request<TokenResponse>("/auth/me/password", {
      method: "POST",
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),
  identities: () => request<AuthUser["identities"]>("/auth/me/identities"),
  unbindIdentity: (id: number) => request<void>(`/auth/me/identities/${id}`, { method: "DELETE" }),
  wechatUrl: (redirect = "/", bind = false) =>
    request<{ url: string; state: string; mode: string }>(
      `/auth/wechat/url?redirect=${encodeURIComponent(redirect)}&bind=${bind}`,
    ),
  exchangeTicket: (ticket: string) =>
    request<TokenResponse>("/auth/ticket", { method: "POST", body: JSON.stringify({ ticket }) }),

  // --- users (admin) ---
  users: (params: { q?: string; page?: number; page_size?: number; role?: string; status?: string } = {}) => {
    const search = new URLSearchParams();
    if (params.q) search.set("q", params.q);
    if (params.role) search.set("role", params.role);
    if (params.status) search.set("status", params.status);
    search.set("page", String(params.page ?? 1));
    search.set("page_size", String(params.page_size ?? 50));
    return request<UserPage>(`/users?${search}`);
  },
  createUser: (payload: Record<string, unknown>) =>
    request<AuthUser>("/users", { method: "POST", body: JSON.stringify(payload) }),
  updateUser: (id: number, payload: Record<string, unknown>) =>
    request<AuthUser>(`/users/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  resetUserPassword: (id: number, newPassword: string) =>
    request<void>(`/users/${id}/password`, {
      method: "POST",
      body: JSON.stringify({ new_password: newPassword }),
    }),
  deleteUser: (id: number) => request<void>(`/users/${id}`, { method: "DELETE" }),
  auditLogs: (limit = 100, action = "") => {
    const search = new URLSearchParams({ limit: String(limit) });
    if (action) search.set("action", action);
    return request<AuditLog[]>(`/users/audit-logs?${search}`);
  },

  // --- workspaces (isolation boundary + roles) ---
  workspaces: (all = false) => request<Workspace[]>(`/workspaces${all ? "?all=true" : ""}`),
  createWorkspace: (payload: { name: string; description?: string }) =>
    request<Workspace>("/workspaces", { method: "POST", body: JSON.stringify(payload) }),
  updateWorkspace: (id: number, payload: { name?: string; description?: string; message_template?: string }) =>
    request<Workspace>(`/workspaces/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteWorkspace: (id: number, purge = false) =>
    request<{ status: string; purged: Record<string, number> }>(`/workspaces/${id}?purge=${purge}`, {
      method: "DELETE",
    }),
  workspaceStats: (id: number) => request<WorkspaceUsage>(`/workspaces/${id}/stats`),
  workspaceUsageHistory: (id: number, days = 30) =>
    request<WorkspaceUsagePoint[]>(`/workspaces/${id}/stats/history?days=${days}`),
  downloadWorkspaceUsage: async (id: number) => {
    const response = await requestRaw(`/workspaces/${id}/stats/export`);
    // 文件名以后端 Content-Disposition 为准（含导出日期），取不到再回退本地命名。
    const disposition = response.headers.get("Content-Disposition") ?? "";
    const match = /filename\*?=(?:UTF-8'')?"?([^";]+)"?/i.exec(disposition);
    let filename = `workspace-${id}-usage.csv`;
    if (match?.[1]) {
      try {
        filename = decodeURIComponent(match[1]);
      } catch {
        filename = match[1];
      }
    }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  },
  workspaceMembers: (id: number) => request<WorkspaceMember[]>(`/workspaces/${id}/members`),
  workspaceCandidates: (id: number, search = "") =>
    request<WorkspaceMember[]>(`/workspaces/${id}/members/candidates?search=${encodeURIComponent(search)}`),
  addWorkspaceMember: (id: number, username: string, role: WorkspaceRole) =>
    request<WorkspaceMember>(`/workspaces/${id}/members`, {
      method: "POST",
      body: JSON.stringify({ username, role }),
    }),
  updateWorkspaceMember: (id: number, userId: number, role: WorkspaceRole) =>
    request<WorkspaceMember>(`/workspaces/${id}/members/${userId}`, {
      method: "PATCH",
      body: JSON.stringify({ role }),
    }),
  removeWorkspaceMember: (id: number, userId: number) =>
    request<void>(`/workspaces/${id}/members/${userId}`, { method: "DELETE" }),
  transferWorkspace: (id: number, userId: number) =>
    request<WorkspaceMember>(`/workspaces/${id}/transfer`, {
      method: "POST",
      body: JSON.stringify({ user_id: userId }),
    }),

  // --- workspaces: 通知通道（地址读接口已脱敏；写操作仅 owner） ---
  workspaceChannels: (id: number) => request<WorkspaceChannel[]>(`/workspaces/${id}/channels`),
  createWorkspaceChannel: (
    id: number,
    payload: { name: string; kind: WorkspaceChannelKind; url: string; enabled?: boolean },
  ) =>
    request<WorkspaceChannel>(`/workspaces/${id}/channels`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateWorkspaceChannel: (
    id: number,
    channelId: number,
    payload: { name?: string; kind?: WorkspaceChannelKind; url?: string; enabled?: boolean },
  ) =>
    request<WorkspaceChannel>(`/workspaces/${id}/channels/${channelId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteWorkspaceChannel: (id: number, channelId: number) =>
    request<void>(`/workspaces/${id}/channels/${channelId}`, { method: "DELETE" }),
  testWorkspaceChannel: (id: number, channelId: number) =>
    request<WorkspaceChannelTestResult>(`/workspaces/${id}/channels/${channelId}/test`, {
      method: "POST",
    }),

  // --- workspaces: 预警策略（成员只读生效值；PUT 仅 owner，null=跟随规则） ---
  alertPolicy: (id: number) => request<AlertPolicy>(`/workspaces/${id}/alert-policy`),
  updateAlertPolicy: (
    id: number,
    payload: {
      exclude_st: boolean | null;
      min_amount: number | null;
      cooldown_seconds: number | null;
      once_per_day: boolean | null;
    },
  ) =>
    request<AlertPolicy>(`/workspaces/${id}/alert-policy`, {
      method: "PUT",
      body: JSON.stringify(payload),
    }),

  // --- admin: system backup / restore ---
  backups: () => request<BackupList>("/admin/backups"),
  createBackup: (note = "", includeModels = true, includeSecret = true) => {
    const search = new URLSearchParams({
      note,
      include_models: String(includeModels),
      include_secret: String(includeSecret),
    });
    return request<BackupCreated>(`/admin/backups?${search}`, { method: "POST" });
  },
  deleteBackup: (name: string) =>
    request<void>(`/admin/backups/${encodeURIComponent(name)}`, { method: "DELETE" }),
  downloadBackup: async (name: string) => {
    const response = await requestRaw(`/admin/backups/${encodeURIComponent(name)}`);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  },
  inspectRestore: async (file: File) => {
    const response = await requestRaw("/admin/restore/inspect", { method: "POST", body: file });
    return (await response.json()) as RestoreInspection;
  },
  restoreUpload: async (file: File, confirm: boolean) => {
    const search = new URLSearchParams({ confirm: String(confirm), filename: file.name });
    const response = await requestRaw(`/admin/restore?${search}`, { method: "POST", body: file });
    return (await response.json()) as { status: string; message: string; report: Record<string, unknown> };
  },

  // --- market ---
  health: () => request<{ status: string; version: string }>("/health"),
  listStocks: (params: { q?: string; limit?: number; include_indices?: boolean } = {}) => {
    const search = new URLSearchParams();
    if (params.q) search.set("q", params.q);
    search.set("limit", String(params.limit ?? 100));
    if (params.include_indices) search.set("include_indices", "true");
    return request<{ total: number; items: Instrument[] }>(`/stocks?${search}`);
  },
  bars: (code: string, params: { adjust?: string; limit?: number; start?: string; end?: string } = {}) => {
    const search = new URLSearchParams();
    search.set("adjust", params.adjust ?? "qfq");
    search.set("limit", String(params.limit ?? 500));
    if (params.start) search.set("start", params.start);
    if (params.end) search.set("end", params.end);
    return request<BarsResponse>(`/stocks/${code}/bars?${search}`);
  },
  overview: () => request<MarketOverview>("/market/overview"),
  indicators: (code: string, limit = 260) =>
    request<IndicatorSnapshot>(`/stocks/${code}/indicators?limit=${limit}`),
  calendar: (start: string, end: string) =>
    request<{ days: string[] }>(`/calendar?start=${start}&end=${end}`),

  // --- analysis ---
  factors: (code: string) => request<FactorSnapshot>(`/analysis/factors/${code}`),
  screen: (payload: {
    min_score?: number;
    max_vol?: number;
    exclude_st?: boolean;
    limit?: number;
  }) =>
    request<ScreenRow[]>("/analysis/screen", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  correlation: (codes: string[], window = 120) =>
    request<{ codes: string[]; matrix: Array<Array<number | null>> }>(
      `/analysis/correlation?codes=${codes.join(",")}&window=${window}`,
    ),

  // --- prediction ---
  models: (horizon?: number) =>
    request<ModelCard[]>(`/predict/models${horizon ? `?horizon=${horizon}` : ""}`),
  train: (payload: { algo: string; horizon?: number }) =>
    request<Record<string, unknown>>("/predict/train", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  predictRun: (horizon = 1) =>
    request<{ model_id: string; scored: number }>(`/predict/run?horizon=${horizon}`, {
      method: "POST",
    }),
  predictions: (horizon = 1, limit = 100) =>
    request<{ trade_date: string | null; items: PredictionOut[] }>(
      `/predict?horizon=${horizon}&limit=${limit}`,
    ),
  predictionDetail: (code: string, horizon = 1) =>
    request<Record<string, unknown>>(`/stocks/${code}/prediction?horizon=${horizon}`),

  // --- backtest ---
  runBacktest: (payload: Record<string, unknown>) =>
    request<BacktestResponse>("/backtest/run", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  backtests: () =>
    request<Array<{ run_id: string; strategy: string; status: string; metrics: Record<string, unknown> }>>(
      "/backtest",
    ),
  backtest: (runId: string) =>
    request<BacktestResponse & { config: Record<string, unknown> }>(`/backtest/${runId}`),

  // --- alerts ---
  ruleTypes: () => request<RuleTypeInfo[]>("/alerts/types"),
  alertRules: () => request<AlertRule[]>("/alerts/rules"),
  createRule: (payload: Record<string, unknown>) =>
    request<AlertRule>("/alerts/rules", { method: "POST", body: JSON.stringify(payload) }),
  updateRule: (id: number, payload: Record<string, unknown>) =>
    request<AlertRule>(`/alerts/rules/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteRule: (id: number) => request<void>(`/alerts/rules/${id}`, { method: "DELETE" }),
  triggers: (status?: string) =>
    request<AlertTrigger[]>(`/alerts/triggers${status ? `?status=${status}` : ""}`),
  updateTrigger: (id: number, status: string) =>
    request<AlertTrigger>(`/alerts/triggers/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ status }),
    }),
  scanAlerts: () =>
    request<{ triggered: number; items: AlertTrigger[] }>("/alerts/scan", { method: "POST" }),
  previewRule: (payload: Record<string, unknown>) =>
    request<{ matched: number; scope_size: number; items: Array<{ code: string; message: string }> }>(
      "/alerts/preview",
      { method: "POST", body: JSON.stringify(payload) },
    ),

  // --- watchlist ---
  watchlist: (listName = "default") =>
    request<WatchlistItem[]>(`/watchlist?list_name=${encodeURIComponent(listName)}`),
  addWatch: (code: string, listName = "default", note = "") =>
    request<WatchlistItem>("/watchlist", {
      method: "POST",
      body: JSON.stringify({ code, list_name: listName, note }),
    }),
  removeWatch: (id: number) => request<void>(`/watchlist/${id}`, { method: "DELETE" }),
  watchlistLists: () => request<WatchlistList[]>("/watchlist/lists"),
  renameWatchlistList: (listName: string, newName: string) =>
    request<WatchlistRenameResult>(`/watchlist/lists/${encodeURIComponent(listName)}`, {
      method: "PATCH",
      body: JSON.stringify({ new_name: newName }),
    }),
  deleteWatchlistList: (listName: string) =>
    request<WatchlistDeleteResult>(`/watchlist/lists/${encodeURIComponent(listName)}`, {
      method: "DELETE",
    }),
  batchRemoveWatch: (ids: number[]) =>
    request<WatchlistBatchDeleteResult>("/watchlist/batch-delete", {
      method: "POST",
      body: JSON.stringify({ ids }),
    }),
  importWatchlist: (payload: {
    list_name?: string;
    codes: string[];
    notes?: Record<string, string>;
    create_list?: boolean;
    skip_existing?: boolean;
  }) =>
    request<WatchlistImportResult>("/watchlist/import", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  /** 批量行情：一次请求取多只（快照优先，失败回退日线）。 */
  quotes: (codes: string[]) =>
    request<QuotesResponse>(`/market/quotes?codes=${encodeURIComponent(codes.join(","))}`),
  /** 批量因子评分：取最新因子日的 score。 */
  factorScores: (codes: string[]) =>
    request<FactorScoresResponse>(`/analysis/factors?codes=${encodeURIComponent(codes.join(","))}`),

  // --- strategy configuration (workspace-scoped 选股 / 样本 / 算法) ---
  screeningConfigs: () => request<ScreeningConfig[]>("/strategy/screenings"),
  screeningConfig: (id: number) => request<ScreeningConfig>(`/strategy/screenings/${id}`),
  createScreeningConfig: (payload: ScreeningConfigInput) =>
    request<ScreeningConfig>("/strategy/screenings", { method: "POST", body: JSON.stringify(payload) }),
  updateScreeningConfig: (id: number, payload: Partial<ScreeningConfigInput>) =>
    request<ScreeningConfig>(`/strategy/screenings/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteScreeningConfig: (id: number) => request<void>(`/strategy/screenings/${id}`, { method: "DELETE" }),
  runScreeningConfig: (id: number) =>
    request<ScreeningRunResult>(`/strategy/screenings/${id}/run`, { method: "POST" }),
  sampleConfigs: () => request<SampleConfig[]>("/strategy/samples"),
  sampleConfig: (id: number) => request<SampleConfig>(`/strategy/samples/${id}`),
  createSampleConfig: (payload: SampleConfigInput) =>
    request<SampleConfig>("/strategy/samples", { method: "POST", body: JSON.stringify(payload) }),
  updateSampleConfig: (id: number, payload: Partial<SampleConfigInput>) =>
    request<SampleConfig>(`/strategy/samples/${id}`, { method: "PATCH", body: JSON.stringify(payload) }),
  deleteSampleConfig: (id: number) => request<void>(`/strategy/samples/${id}`, { method: "DELETE" }),
  resolveSampleConfig: (id: number) =>
    request<SampleResolveResult>(`/strategy/samples/${id}/resolve`, { method: "POST" }),
  algorithmConfigs: () => request<AlgorithmConfig[]>("/strategy/algorithms"),
  algorithmConfig: (id: number) => request<AlgorithmConfig>(`/strategy/algorithms/${id}`),
  createAlgorithmConfig: (payload: AlgorithmConfigInput) =>
    request<AlgorithmConfig>("/strategy/algorithms", { method: "POST", body: JSON.stringify(payload) }),
  updateAlgorithmConfig: (id: number, payload: Partial<AlgorithmConfigInput>) =>
    request<AlgorithmConfig>(`/strategy/algorithms/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteAlgorithmConfig: (id: number) => request<void>(`/strategy/algorithms/${id}`, { method: "DELETE" }),
  trainAlgorithmConfig: (id: number) =>
    request<AlgorithmTrainResult>(`/strategy/algorithms/${id}/train`, { method: "POST" }),

  // --- system ---
  status: () => request<SystemStatus>("/system/status"),
  runJob: (job: string, params: Record<string, string | number | boolean> = {}) => {
    const search = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => search.set(key, String(value)));
    const query = search.toString();
    return request<{ job: string; detail: Record<string, unknown> }>(
      `/system/jobs/${job}${query ? `?${query}` : ""}`,
      { method: "POST" },
    );
  },
};

export type { TradeOut };
