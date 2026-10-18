import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import AddToWatchlistDialog from "../components/AddToWatchlistDialog";
import KlineChart from "../components/KlineChart";
import { formatCompact, formatNumber, formatPct, changeColor } from "../format";
import { canWriteWorkspace, useCurrentWorkspace } from "../workspace";
import { useViewState } from "../view";

export default function Market() {
  const queryClient = useQueryClient();
  const { search, selected, adjust } = useViewState((state) => state.market);
  const setMarket = useViewState((state) => state.setMarket);
  const workspace = useCurrentWorkspace();
  const readOnly = !canWriteWorkspace(workspace);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [watchNotice, setWatchNotice] = useState("");

  const stocks = useQuery({
    queryKey: ["stocks", search],
    queryFn: () => api.listStocks({ q: search || undefined, limit: 200 }),
  });

  const bars = useQuery({
    queryKey: ["bars", selected, adjust],
    queryFn: () => api.bars(selected, { adjust, limit: 500 }),
    enabled: Boolean(selected),
  });

  const indicators = useQuery({
    queryKey: ["indicators", selected],
    queryFn: () => api.indicators(selected),
    enabled: Boolean(selected),
  });

  const watchlist = useQuery({ queryKey: ["watchlist"], queryFn: () => api.watchlist() });

  const addWatch = useMutation({
    mutationFn: (code: string) => api.addWatch(code),
    onSuccess: (_item, code) => {
      setWatchNotice(`已将 ${code} 加入自选`);
      void queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      void queryClient.invalidateQueries({ queryKey: ["watchlist-lists"] });
    },
    onError: () => setWatchNotice(""),
  });

  const latest = bars.data?.bars?.[bars.data.bars.length - 1];
  const previous = bars.data?.bars?.[bars.data.bars.length - 2];
  const change = useMemo(() => {
    if (!latest) return null;
    if (latest.pct_chg !== null && latest.pct_chg !== undefined) return latest.pct_chg;
    if (previous && previous.close) return (latest.close / previous.close - 1) * 100;
    return null;
  }, [latest, previous]);

  const watched = new Set((watchlist.data ?? []).map((item) => item.code));

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <input
          className="input w-full sm:w-56"
          placeholder="搜索代码或名称，如 600519 / 茅台"
          value={search}
          onChange={(event) => setMarket({ search: event.target.value })}
        />
        <select className="input" value={adjust} onChange={(e) => setMarket({ adjust: e.target.value })}>
          <option value="qfq">前复权 (qfq)</option>
          <option value="raw">不复权 (raw)</option>
        </select>
        {bars.data && (
          <span className="text-sm text-slate-400">
            {bars.data.name} <span className="text-slate-500">{bars.data.code}</span> ·{" "}
            {bars.data.count} 根日线
          </span>
        )}
        <button
          type="button"
          className="btn"
          disabled={!selected || watched.has(selected) || readOnly}
          title={readOnly ? "查看者无法修改自选" : undefined}
          onClick={() => {
            setWatchNotice("");
            addWatch.mutate(selected);
          }}
        >
          {watched.has(selected) ? "已在自选" : "加入自选"}
        </button>
        {!readOnly && (
          <button
            type="button"
            className="btn"
            disabled={!selected}
            onClick={() => setPickerOpen(true)}
          >
            选择清单…
          </button>
        )}
        {watchNotice && (
          <span className="text-xs text-sky-300" role="status">
            {watchNotice}
          </span>
        )}
        {addWatch.isError && (
          <span className="text-xs text-red-400">{(addWatch.error as Error).message}</span>
        )}
      </div>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[280px_1fr]">
        {/* Symbol list */}
        <div className="card max-h-56 overflow-auto p-0 xl:max-h-[560px]">
          <div className="table-wrap">
            <table className="w-full">
              <thead className="sticky top-0 bg-slate-900">
                <tr>
                  <th className="th">代码</th>
                  <th className="th">名称</th>
                </tr>
              </thead>
              <tbody>
                {(stocks.data?.items ?? []).map((item) => (
                  <tr
                    key={item.code}
                    onClick={() => setMarket({ selected: item.code })}
                    className={`cursor-pointer border-t border-slate-800/60 hover:bg-slate-800/50 ${
                      item.code === selected ? "bg-sky-950/40" : ""
                    }`}
                  >
                    <td className="td">{item.code}</td>
                    <td className="td text-slate-400">{item.name}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {stocks.isLoading && <div className="p-3 text-xs text-slate-500">加载中…</div>}
        </div>

        <div className="space-y-4">
          {bars.isError && (
            <div className="card text-sm text-amber-300">
              {(bars.error as Error).message}
            </div>
          )}

          {latest && (
            <div className="flex flex-wrap items-baseline gap-x-6 gap-y-1">
              <span className={`text-2xl font-semibold ${changeColor(change)}`}>
                {formatNumber(latest.close)}
              </span>
              <span className={`text-sm ${changeColor(change)}`}>{formatPct(change)}</span>
              <span className="text-xs text-slate-400">
                开 {formatNumber(latest.open)} · 高 {formatNumber(latest.high)} · 低{" "}
                {formatNumber(latest.low)}
              </span>
              <span className="text-xs text-slate-400">
                量 {formatCompact(latest.volume)} · 额 {formatCompact(latest.amount)}
              </span>
              <span className="text-xs text-slate-400">
                换手 {latest.turn === null ? "--" : `${latest.turn.toFixed(2)}%`}
              </span>
              {latest.is_st && <span className="badge bg-red-900/60 text-red-300">ST</span>}
            </div>
          )}

          {bars.data?.bars?.length ? (
            <div className="card">
              <KlineChart
                bars={bars.data.bars}
                indicators={indicators.data}
                height="clamp(360px, 62vh, 480px)"
              />
            </div>
          ) : (
            !bars.isLoading && !bars.isError && (
              <div className="card text-sm text-slate-500">暂无数据</div>
            )
          )}

          {indicators.data && (
            <div className="card">
              <div className="mb-2 text-sm font-medium text-slate-300">
                技术指标 <span className="text-xs text-slate-500">({indicators.data.trade_date})</span>
              </div>
              <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm md:grid-cols-4">
                {Object.entries(indicators.data.values).map(([key, value]) => (
                  <div key={key} className="flex justify-between border-b border-slate-800/50 py-0.5">
                    <span className="text-slate-400">{key}</span>
                    <span className="text-slate-200">
                      {value === null ? "--" : value.toFixed(3)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>

      {pickerOpen && selected && (
        <AddToWatchlistDialog
          open
          codes={[selected]}
          names={bars.data?.name ? { [selected]: bars.data.name } : undefined}
          onClose={() => setPickerOpen(false)}
        />
      )}
    </div>
  );
}
