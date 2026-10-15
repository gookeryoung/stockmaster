/** Minimal typed API client.
 *
 * Deliberately dependency-free (fetch + a small helper) so the surface stays
 * small. Errors are converted into thrown `ApiError`s carrying the backend's
 * `detail` message, because the API returns actionable messages (for example
 * "run the analytics job first") that the UI should surface verbatim.
 */

import type {
  AlertRule,
  AlertTrigger,
  BacktestResponse,
  BarsResponse,
  FactorSnapshot,
  IndicatorSnapshot,
  Instrument,
  MarketOverview,
  ModelCard,
  PredictionOut,
  RuleTypeInfo,
  ScreenRow,
  SystemStatus,
  TradeOut,
  WatchlistItem,
} from "./types";

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

const BASE = "/api/v1";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });

  if (!response.ok) {
    let detail = `请求失败 (HTTP ${response.status})`;
    try {
      const body = await response.json();
      if (typeof body?.detail === "string") {
        detail = body.detail;
      } else if (Array.isArray(body?.detail)) {
        // FastAPI validation errors arrive as a list of objects.
        detail = body.detail
          .map((item: { loc?: string[]; msg?: string }) =>
            `${item.loc?.slice(1).join(".") ?? ""}: ${item.msg ?? ""}`.trim(),
          )
          .join("; ");
      }
    } catch {
      // Body was not JSON; keep the generic message.
    }
    throw new ApiError(response.status, detail);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export const api = {
  // --- market ---
  health: () => request<{ status: string; version: string }>("/health"),
  listStocks: (params: { q?: string; limit?: number; include_indices?: boolean } = {}) => {
    const search = new URLSearchParams();
    if (params.q) search.set("q", params.q);
    search.set("limit", String(params.limit ?? 100));
    if (params.include_indices) search.set("include_indices", "true");
    return request<{ total: number; items: Instrument[] }>(`/stocks?${search}`);
  },
  bars: (code: string, params: { adjust?: string; limit?: number } = {}) => {
    const search = new URLSearchParams();
    search.set("adjust", params.adjust ?? "qfq");
    search.set("limit", String(params.limit ?? 500));
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
    request<WatchlistItem[]>(`/watchlist?list_name=${listName}`),
  addWatch: (code: string, listName = "default", note = "") =>
    request<WatchlistItem>("/watchlist", {
      method: "POST",
      body: JSON.stringify({ code, list_name: listName, note }),
    }),
  removeWatch: (id: number) => request<void>(`/watchlist/${id}`, { method: "DELETE" }),

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
