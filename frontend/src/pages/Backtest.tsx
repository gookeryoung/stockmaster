import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import type { EChartsOption } from "echarts";
import { api } from "../api";
import EChart from "../components/EChart";
import KlineChart, { type KlineMarker } from "../components/KlineChart";
import { formatNumber, formatPct, sideColor } from "../format";
import { canWriteWorkspace, useCurrentWorkspace } from "../workspace";
import { useViewState } from "../view";
import type { BacktestResponse } from "../types";

const STRATEGIES = [
  { value: "buy_hold", label: "买入持有（基准）" },
  { value: "ma_cross", label: "双均线金叉/死叉" },
  { value: "score_top_n", label: "因子评分 Top N（需先运行分析任务）" },
  { value: "prediction_top_n", label: "预测概率 Top N（需先训练并预测）" },
];

export default function Backtest() {
  const workspace = useCurrentWorkspace();
  const readOnly = !canWriteWorkspace(workspace);
  const { strategy, codes, start, end, cash, topN, rebalanceDays, commission, slippage } =
    useViewState((state) => state.backtest);
  const setBacktest = useViewState((state) => state.setBacktest);
  const [result, setResult] = useState<BacktestResponse | null>(null);

  const history = useQuery({ queryKey: ["backtests"], queryFn: api.backtests });

  // 回测结果的买卖点 K 线图：只在成交过的标的中选择，区间取自净值曲线。
  const tradedCodes = useMemo(() => {
    const seen: string[] = [];
    for (const trade of result?.trades ?? []) {
      if (trade.shares > 0 && !seen.includes(trade.code)) seen.push(trade.code);
    }
    return seen;
  }, [result]);

  const [chartCode, setChartCode] = useState("");
  useEffect(() => {
    if (!tradedCodes.length) {
      setChartCode("");
      return;
    }
    setChartCode((current) => (tradedCodes.includes(current) ? current : tradedCodes[0]));
  }, [tradedCodes]);

  const chartWindow =
    result && result.equity.length
      ? { start: result.equity[0].trade_date, end: result.equity[result.equity.length - 1].trade_date }
      : null;

  const chartBars = useQuery({
    queryKey: ["backtest-bars", chartCode, chartWindow?.start, chartWindow?.end],
    queryFn: () =>
      api.bars(chartCode, {
        adjust: "qfq",
        limit: 20000,
        start: chartWindow?.start,
        end: chartWindow?.end,
      }),
    enabled: Boolean(chartCode) && Boolean(chartWindow),
  });

  const chartMarkers = useMemo<KlineMarker[]>(() => {
    if (!chartCode) return [];
    return (result?.trades ?? [])
      .filter((trade) => trade.code === chartCode && trade.shares > 0 && trade.price > 0)
      .map((trade) => ({ trade_date: trade.trade_date, price: trade.price, side: trade.side }));
  }, [result, chartCode]);

  const run = useMutation({
    mutationFn: () =>
      api.runBacktest({
        strategy,
        codes: codes
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean),
        start,
        end,
        initial_cash: cash,
        top_n: topN,
        rebalance_days: rebalanceDays,
        commission_rate: commission,
        slippage_rate: slippage,
        benchmark: "000300.SH",
      }),
    onSuccess: (data) => setResult(data),
  });

  const equityOption = useMemo<EChartsOption | null>(() => {
    if (!result) return null;
    const dates = result.equity.map((point) => point.trade_date);
    return {
      backgroundColor: "transparent",
      tooltip: { trigger: "axis" },
      legend: { data: ["策略净值", "沪深300", "回撤"], textStyle: { color: "#94a3b8" } },
      grid: [
        { left: 70, right: 30, top: 30, height: "52%" },
        { left: 70, right: 30, top: "70%", height: "22%" },
      ],
      xAxis: [
        {
          type: "category",
          data: dates,
          axisLabel: { color: "#94a3b8" },
          axisLine: { lineStyle: { color: "#334155" } },
        },
        {
          type: "category",
          data: dates,
          gridIndex: 1,
          axisLabel: { color: "#94a3b8" },
          axisLine: { lineStyle: { color: "#334155" } },
        },
      ],
      yAxis: [
        {
          type: "value",
          scale: true,
          splitLine: { lineStyle: { color: "#1e293b" } },
          axisLabel: { color: "#94a3b8" },
        },
        {
          gridIndex: 1,
          type: "value",
          axisLabel: { color: "#94a3b8", formatter: "{value}%" },
          splitLine: { lineStyle: { color: "#1e293b" } },
        },
      ],
      series: [
        {
          name: "策略净值",
          type: "line",
          data: result.equity.map((point) => point.equity),
          showSymbol: false,
          lineStyle: { color: "#ef4444", width: 1.5 },
        },
        {
          name: "沪深300",
          type: "line",
          data: result.equity.map((point) => point.benchmark),
          showSymbol: false,
          lineStyle: { color: "#38bdf8", width: 1.2 },
        },
        {
          name: "回撤",
          type: "line",
          data: result.equity.map((point) => Number((point.drawdown * 100).toFixed(3))),
          showSymbol: false,
          xAxisIndex: 1,
          yAxisIndex: 1,
          areaStyle: { color: "rgba(34,197,94,0.25)" },
          lineStyle: { color: "#22c55e", width: 1 },
        },
      ],
      dataZoom: [{ type: "inside", xAxisIndex: [0, 1] }],
    };
  }, [result]);

  const metrics = (result?.metrics ?? {}) as Record<string, number | null | undefined>;

  return (
    <div className="space-y-4">
      <div className="card space-y-3">
        <div className="text-sm font-medium text-slate-300">回测参数</div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-4">
          <div className="sm:col-span-2">
            <label className="label">策略</label>
            <select
              className="input w-full"
              value={strategy}
              onChange={(e) => setBacktest({ strategy: e.target.value })}
            >
              {STRATEGIES.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>
          <div className="sm:col-span-2">
            <label className="label">标的（逗号分隔）</label>
            <input className="input w-full" value={codes} onChange={(e) => setBacktest({ codes: e.target.value })} />
          </div>
          <div>
            <label className="label">开始日期</label>
            <input type="date" className="input w-full" value={start} onChange={(e) => setBacktest({ start: e.target.value })} />
          </div>
          <div>
            <label className="label">结束日期</label>
            <input type="date" className="input w-full" value={end} onChange={(e) => setBacktest({ end: e.target.value })} />
          </div>
          <div>
            <label className="label">初始资金</label>
            <input
              type="number"
              className="input w-full"
              value={cash}
              onChange={(e) => setBacktest({ cash: Number(e.target.value) })}
            />
          </div>
          <div>
            <label className="label">持仓数量 Top N</label>
            <input
              type="number"
              className="input w-full"
              value={topN}
              onChange={(e) => setBacktest({ topN: Number(e.target.value) })}
            />
          </div>
          <div>
            <label className="label">调仓间隔（交易日）</label>
            <input
              type="number"
              className="input w-full"
              value={rebalanceDays}
              onChange={(e) => setBacktest({ rebalanceDays: Number(e.target.value) })}
            />
          </div>
          <div>
            <label className="label">佣金率</label>
            <input
              type="number"
              step="0.00001"
              className="input w-full"
              value={commission}
              onChange={(e) => setBacktest({ commission: Number(e.target.value) })}
            />
          </div>
          <div>
            <label className="label">滑点率</label>
            <input
              type="number"
              step="0.0001"
              className="input w-full"
              value={slippage}
              onChange={(e) => setBacktest({ slippage: Number(e.target.value) })}
            />
          </div>
        </div>
        <div className="flex items-center gap-2">
          {readOnly && (
            <span className="text-xs text-amber-300">
              当前工作区为只读角色（viewer），仅可查看历史回测，不能发起回测。
            </span>
          )}
          <button
            className="btn btn-primary"
            onClick={() => run.mutate()}
            disabled={run.isPending || readOnly}
          >
            {run.isPending ? "回测中…" : "开始回测"}
          </button>
          <span className="text-xs text-slate-500">
            已内置 T+1、涨跌停不可成交、印花税（卖出）、过户费与最低佣金 5 元。
          </span>
        </div>
        {run.isError && <div className="text-sm text-amber-300">{(run.error as Error).message}</div>}
      </div>

      {result && (
        <>
          <div className="card">
            <div className="mb-2 text-sm font-medium text-slate-300">绩效指标</div>
            <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm md:grid-cols-4">
              <Stat label="总收益" value={metrics.total_return} pct />
              <Stat label="年化收益" value={metrics.annualised_return} pct />
              <Stat label="最大回撤" value={metrics.max_drawdown} pct />
              <Stat label="夏普" value={metrics.sharpe} />
              <Stat label="索提诺" value={metrics.sortino} />
              <Stat label="卡玛" value={metrics.calmar} />
              <Stat label="胜率" value={metrics.win_rate} pct />
              <Stat label="盈亏比" value={metrics.profit_factor} />
              <Stat label="换手" value={metrics.turnover} />
              <Stat label="成交笔数" value={metrics.n_trades} digits={0} />
              <Stat label="交易成本" value={metrics.total_costs} digits={2} />
              <Stat label="基准收益" value={metrics.benchmark_return} pct />
              <Stat label="超额收益" value={metrics.excess_return} pct />
            </div>
            {metrics.skip_reasons && (
              <div className="mt-3 text-xs text-slate-500">
                未成交原因：{JSON.stringify(metrics.skip_reasons)}
              </div>
            )}
            <p className="mt-3 rounded border border-amber-900/50 bg-amber-950/30 p-2 text-xs text-amber-300">
              {result.disclaimer}
            </p>
          </div>

          {equityOption && (
            <div className="card">
              <div className="mb-2 text-sm font-medium text-slate-300">净值与回撤曲线</div>
              <EChart option={equityOption} height="clamp(320px, 58vh, 520px)" />
            </div>
          )}

          {tradedCodes.length > 0 && (
            <div className="card">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <div className="text-sm font-medium text-slate-300">买卖点 K 线图</div>
                <select
                  className="input"
                  aria-label="K线图标的"
                  value={chartCode}
                  onChange={(e) => setChartCode(e.target.value)}
                >
                  {tradedCodes.map((code) => (
                    <option key={code} value={code}>
                      {code}
                    </option>
                  ))}
                </select>
              </div>
              {chartBars.isError ? (
                <div className="text-sm text-amber-300">{(chartBars.error as Error).message}</div>
              ) : chartBars.data?.bars?.length ? (
                <KlineChart
                  bars={chartBars.data.bars}
                  markers={chartMarkers}
                  showMacd={false}
                  height="clamp(340px, 60vh, 540px)"
                />
              ) : (
                !chartBars.isLoading && <div className="text-sm text-slate-500">暂无该标的的日线数据。</div>
              )}
              <p className="mt-2 text-xs text-slate-500">
                红三角为买入、绿三角为卖出（前复权价），仅标注回测区间内该标的的成交点。
              </p>
            </div>
          )}

          <div className="card">
            <div className="mb-2 text-sm font-medium text-slate-300">
              成交明细 <span className="text-xs text-slate-500">共 {result.trades.length} 条</span>
            </div>
            <div className="max-h-96 overflow-auto">
              <div className="table-wrap">
                <table className="w-full">
                  <thead className="sticky top-0 bg-slate-900">
                    <tr>
                      <th className="th">日期</th>
                      <th className="th">代码</th>
                      <th className="th">方向</th>
                      <th className="th text-right">价格</th>
                      <th className="th text-right">数量</th>
                      <th className="th text-right">金额</th>
                      <th className="th text-right">佣金</th>
                      <th className="th text-right">印花税</th>
                      <th className="th text-right">盈亏</th>
                      <th className="th">备注</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.trades.map((trade, index) => (
                      <tr key={`${trade.trade_date}-${trade.code}-${index}`} className="border-t border-slate-800/60">
                        <td className="td text-slate-400">{trade.trade_date}</td>
                        <td className="td">{trade.code}</td>
                        <td className={`td ${sideColor(trade.side)}`}>
                          {trade.side === "buy" ? "买入" : "卖出"}
                        </td>
                        <td className="td text-right">{formatNumber(trade.price, 3)}</td>
                        <td className="td text-right">{trade.shares}</td>
                        <td className="td text-right">{formatNumber(trade.gross, 2)}</td>
                        <td className="td text-right">{formatNumber(trade.commission, 2)}</td>
                        <td className="td text-right">{formatNumber(trade.stamp_tax, 2)}</td>
                        <td className={`td text-right ${trade.pnl === null ? "" : sideColor(trade.pnl > 0 ? "buy" : "sell")}`}>
                          {trade.pnl === null ? "--" : formatNumber(trade.pnl, 2)}
                        </td>
                        <td className="td text-slate-500">{trade.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          </div>
        </>
      )}

      <div className="card">
        <div className="mb-2 text-sm font-medium text-slate-300">历史回测记录</div>
        {history.data?.length ? (
          <div className="table-wrap">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-800">
                  <th className="th">运行 ID</th>
                  <th className="th">策略</th>
                  <th className="th text-right">总收益</th>
                  <th className="th text-right">最大回撤</th>
                  <th className="th">状态</th>
                </tr>
              </thead>
              <tbody>
                {history.data.map((item) => (
                  <tr key={item.run_id} className="border-t border-slate-800/60">
                    <td className="td text-slate-500">{item.run_id.slice(0, 8)}</td>
                    <td className="td">{item.strategy}</td>
                    <td className="td text-right">{formatPct(Number(item.metrics?.total_return) * 100)}</td>
                    <td className="td text-right">{formatPct(Number(item.metrics?.max_drawdown) * 100)}</td>
                    <td className="td text-slate-400">{item.status}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-xs text-slate-500">暂无历史记录。</p>
        )}
      </div>
    </div>
  );
}

function Stat({
  label,
  value,
  pct = false,
  digits = 4,
}: {
  label: string;
  value: number | null | undefined;
  pct?: boolean;
  digits?: number;
}) {
  const display =
    value === null || value === undefined || Number.isNaN(Number(value))
      ? "--"
      : pct
        ? formatPct(Number(value) * 100)
        : Number(value).toFixed(digits);
  return (
    <div className="flex justify-between border-b border-slate-800/50 py-0.5">
      <span className="text-slate-400">{label}</span>
      <span className="text-slate-200">{display}</span>
    </div>
  );
}
