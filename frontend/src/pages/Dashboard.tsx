import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../api";
import { changeColor, formatCompact, formatNumber, formatPct } from "../format";

export default function Dashboard() {
  const overview = useQuery({ queryKey: ["overview"], queryFn: api.overview });
  const predictions = useQuery({
    queryKey: ["predictions", 1],
    queryFn: () => api.predictions(1, 10),
  });
  const triggers = useQuery({
    queryKey: ["triggers", "recent"],
    queryFn: () => api.triggers(),
  });
  const watchlist = useQuery({ queryKey: ["watchlist"], queryFn: () => api.watchlist() });
  const previewCodes = (watchlist.data ?? []).slice(0, 8).map((item) => item.code);
  const watchlistQuotes = useQuery({
    queryKey: ["dashboard-watchlist-quotes", previewCodes.join(",")],
    queryFn: () => api.quotes(previewCodes),
    enabled: previewCodes.length > 0,
    staleTime: 60_000,
    retry: false,
  });
  const quoteByCode = new Map((watchlistQuotes.data?.items ?? []).map((row) => [row.code, row]));

  const data = overview.data;

  return (
    <div className="space-y-4">
      {/* Index cards */}
      <section className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
        {(data?.indices ?? []).map((index) => (
          <div key={index.code} className="card">
            <div className="text-xs text-slate-400">
              {index.name} <span className="text-slate-600">{index.code}</span>
            </div>
            <div className="mt-1 text-lg font-semibold text-slate-100">
              {index.close === null ? "--" : index.close.toFixed(2)}
            </div>
            <div className={`text-sm ${changeColor(index.pct_chg)}`}>
              {formatPct(index.pct_chg)}
            </div>
          </div>
        ))}
        {overview.isLoading && <div className="card text-sm text-slate-500">加载中…</div>}
      </section>

      {/* Breadth */}
      <section className="grid grid-cols-1 gap-3 lg:grid-cols-3">
        <div className="card">
          <div className="mb-2 text-sm font-medium text-slate-300">市场宽度</div>
          <div className="grid grid-cols-2 gap-y-2 text-sm">
            <div className="text-slate-400">上涨</div>
            <div className="text-right text-up">{data?.advancing ?? "--"}</div>
            <div className="text-slate-400">下跌</div>
            <div className="text-right text-down">{data?.declining ?? "--"}</div>
            <div className="text-slate-400">平盘</div>
            <div className="text-right text-flat">{data?.unchanged ?? "--"}</div>
            <div className="text-slate-400">涨停</div>
            <div className="text-right text-up">{data?.limit_up ?? "--"}</div>
            <div className="text-slate-400">跌停</div>
            <div className="text-right text-down">{data?.limit_down ?? "--"}</div>
            <div className="text-slate-400">样本数</div>
            <div className="text-right text-slate-200">{data?.total ?? "--"}</div>
          </div>
          <p className="mt-3 text-xs text-slate-500">
            数据日期：{data?.as_of ?? "无"}。若为“无”，请先在
            <Link className="mx-1 text-sky-400 hover:underline" to="/settings">
              设置
            </Link>
            中同步数据。
          </p>
        </div>

        {/* Top predictions */}
        <div className="card">
          <div className="mb-2 flex items-center justify-between">
            <div className="text-sm font-medium text-slate-300">预测上涨概率 Top 10</div>
            <Link to="/predict" className="text-xs text-sky-400 hover:underline">
              详情
            </Link>
          </div>
          {predictions.data?.items?.length ? (
            <div className="table-wrap">
              <table className="w-full">
                <tbody>
                  {predictions.data.items.slice(0, 8).map((item) => (
                    <tr key={item.code} className="border-t border-slate-800/60">
                      <td className="td">{item.code}</td>
                      <td className="td text-slate-400">{item.name}</td>
                      <td className="td text-right text-sky-300">
                        {item.prob_up === null ? "--" : `${(item.prob_up * 100).toFixed(1)}%`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-xs text-slate-500">
              暂无预测。请先在“预测”页训练模型并运行预测。
            </p>
          )}
        </div>

        {/* Watchlist */}
        <div className="card">
          <div className="mb-2 flex items-center justify-between gap-2">
            <div className="flex items-baseline gap-2">
              <div className="text-sm font-medium text-slate-300">自选股</div>
              {watchlist.data?.length ? (
                <span className="text-xs text-slate-500">共 {watchlist.data.length} 只</span>
              ) : null}
            </div>
            <Link to="/watchlist" className="text-xs text-sky-400 hover:underline">
              管理
            </Link>
          </div>
          {watchlist.data?.length ? (
            <div className="table-wrap">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-slate-800">
                    <th className="th">代码</th>
                    <th className="th">名称</th>
                    <th className="th text-right">最新价</th>
                    <th className="th text-right">涨跌幅</th>
                  </tr>
                </thead>
                <tbody>
                  {watchlist.data.slice(0, 8).map((item) => {
                    const quote = quoteByCode.get(item.code);
                    return (
                      <tr key={item.id} className="border-t border-slate-800/60">
                        <td className="td">{item.code}</td>
                        <td className="td text-slate-400">{item.name || "--"}</td>
                        <td className="td text-right text-slate-200">
                          {formatNumber(quote?.price, 2)}
                        </td>
                        <td className={`td text-right ${changeColor(quote?.pct_chg)}`}>
                          {formatPct(quote?.pct_chg)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="text-xs text-slate-500">
              自选股为空，去{" "}
              <Link to="/watchlist" className="text-sky-400 hover:underline">
                自选页
              </Link>{" "}
              添加。
            </p>
          )}
        </div>
      </section>

      {/* Recent alerts */}
      <section className="card">
        <div className="mb-2 flex items-center justify-between">
          <div className="text-sm font-medium text-slate-300">最新预警</div>
          <Link to="/alerts" className="text-xs text-sky-400 hover:underline">
            全部
          </Link>
        </div>
        {triggers.data?.length ? (
          <div className="table-wrap">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-800">
                  <th className="th hidden sm:table-cell">时间</th>
                  <th className="th">代码</th>
                  <th className="th hidden sm:table-cell">规则</th>
                  <th className="th">消息</th>
                </tr>
              </thead>
              <tbody>
                {triggers.data.slice(0, 8).map((item) => (
                  <tr key={item.id} className="border-t border-slate-800/60">
                    <td className="td hidden text-slate-400 sm:table-cell">{item.trade_date}</td>
                    <td className="td">{item.code}</td>
                    <td className="td hidden text-slate-400 sm:table-cell">{item.rule_type}</td>
                    <td className="td min-w-[9rem] whitespace-normal">{item.message}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-xs text-slate-500">
            暂无预警。可在“预警”页创建规则后点击“立即扫描”。
          </p>
        )}
      </section>

      <section className="card text-xs text-slate-400">
        <div className="mb-1 font-medium text-slate-300">数据健康度</div>
        {data?.data_freshness ? (
          <div className="flex flex-wrap gap-x-6 gap-y-1">
            {Object.entries(data.data_freshness).map(([key, value]) => (
              <span key={key}>
                {key}: <span className="text-slate-200">{formatCompact(Number(value)) || String(value)}</span>
              </span>
            ))}
          </div>
        ) : (
          <span>暂无数据</span>
        )}
      </section>
    </div>
  );
}
