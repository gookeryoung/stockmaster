import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api";
import EChart from "../components/EChart";
import { formatNumber, formatPct, changeColor } from "../format";
import type { EChartsOption } from "echarts";

export default function Analyze() {
  const [code, setCode] = useState("600519.SH");
  const [correlationInput, setCorrelationInput] = useState("600519.SH,000001.SZ,300750.SZ");
  const [screenMinScore, setScreenMinScore] = useState<string>("");

  const factors = useQuery({
    queryKey: ["factors", code],
    queryFn: () => api.factors(code),
    retry: false,
  });

  const screen = useMutation({
    mutationFn: () =>
      api.screen({
        limit: 50,
        min_score: screenMinScore === "" ? undefined : Number(screenMinScore),
      }),
  });

  const correlation = useMutation({
    mutationFn: () =>
      api.correlation(
        correlationInput
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean),
      ),
  });

  const radarOption: EChartsOption | null = factors.data
    ? {
        backgroundColor: "transparent",
        tooltip: {},
        radar: {
          indicator: [
            { name: "动量20", max: 3 },
            { name: "动量60", max: 3 },
            { name: "低波动", max: 3 },
            { name: "换手", max: 3 },
            { name: "RSI", max: 3 },
          ],
          axisName: { color: "#94a3b8" },
          splitLine: { lineStyle: { color: "#1e293b" } },
          splitArea: { areaStyle: { color: ["transparent"] } },
        },
        series: [
          {
            type: "radar",
            data: [
              {
                value: [
                  factors.data.factors.z_mom_20 ?? 0,
                  factors.data.factors.z_mom_60 ?? 0,
                  // Volatility is penalised, so invert for a comparable axis.
                  -(factors.data.factors.z_vol_20 ?? 0),
                  factors.data.factors.z_turnover_20 ?? 0,
                  factors.data.factors.z_rsi_14 ?? 0,
                ],
                name: factors.data.code,
                areaStyle: { opacity: 0.25 },
                lineStyle: { color: "#38bdf8" },
              },
            ],
          },
        ],
      }
    : null;

  const heatmapOption: EChartsOption | null = correlation.data
    ? {
        backgroundColor: "transparent",
        tooltip: { position: "top" },
        grid: { left: 90, right: 40, top: 30, bottom: 60 },
        xAxis: {
          type: "category",
          data: correlation.data.codes,
          axisLabel: { color: "#94a3b8", rotate: 30 },
        },
        yAxis: {
          type: "category",
          data: correlation.data.codes,
          axisLabel: { color: "#94a3b8" },
        },
        visualMap: {
          min: -1,
          max: 1,
          calculable: true,
          orient: "horizontal",
          left: "center",
          bottom: 0,
          textStyle: { color: "#94a3b8" },
          inRange: { color: ["#22c55e", "#0f172a", "#ef4444"] },
        },
        series: [
          {
            type: "heatmap",
            data: correlation.data.matrix.flatMap((row, i) =>
              row.map((value, j) => [j, i, value ?? 0]),
            ),
            label: { show: false },
          },
        ],
      }
    : null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <input
          className="input w-full sm:w-48"
          value={code}
          onChange={(event) => setCode(event.target.value.trim().toUpperCase())}
          placeholder="证券代码"
        />
        <span className="text-xs text-slate-500">
          因子由“设置 → 运行分析任务”批量计算后存入数据库
        </span>
      </div>

      {factors.isError && (
        <div className="card text-sm text-amber-300">{(factors.error as Error).message}</div>
      )}

      {factors.data && (
        <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
          <div className="card">
            <div className="mb-2 text-sm font-medium text-slate-300">
              {factors.data.name || factors.data.code} · 因子快照{" "}
              <span className="text-xs text-slate-500">{factors.data.trade_date}</span>
            </div>
            <div className="table-wrap">
              <table className="w-full">
                <tbody>
                  {Object.entries(factors.data.factors).map(([key, value]) => (
                    <tr key={key} className="border-t border-slate-800/50">
                      <td className="td text-slate-400">{key}</td>
                      <td className="td text-right">
                        {value === null ? "--" : value.toFixed(4)}
                      </td>
                    </tr>
                  ))}
                  <tr className="border-t border-slate-700">
                    <td className="td font-medium text-slate-300">综合评分</td>
                    <td className="td text-right font-medium text-sky-300">
                      {factors.data.score === null ? "--" : factors.data.score.toFixed(4)}
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          {radarOption && (
            <div className="card">
              <div className="mb-2 text-sm font-medium text-slate-300">标准化因子雷达图</div>
              <EChart option={radarOption} height={320} />
              <p className="mt-1 text-xs text-slate-500">
                均为横截面 z-score（相对同日全市场），低波动维度已取反以便同向比较。
              </p>
            </div>
          )}
        </div>
      )}

      {/* Screening */}
      <div className="card space-y-3">
        <div className="text-sm font-medium text-slate-300">多因子选股</div>
        <div className="flex flex-wrap items-end gap-2">
          <div>
            <label className="label">最低综合评分</label>
            <input
              className="input w-32"
              value={screenMinScore}
              onChange={(event) => setScreenMinScore(event.target.value)}
              placeholder="如 1.0"
            />
          </div>
          <button
            className="btn btn-primary"
            onClick={() => screen.mutate()}
            disabled={screen.isPending}
          >
            {screen.isPending ? "筛选中…" : "运行筛选"}
          </button>
        </div>

        {screen.isError && (
          <div className="text-sm text-amber-300">{(screen.error as Error).message}</div>
        )}

        {screen.data && (
          <div className="table-wrap">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-800">
                  <th className="th">代码</th>
                  <th className="th">名称</th>
                  <th className="th text-right">评分</th>
                  <th className="th text-right">动量20</th>
                  <th className="th text-right">动量60</th>
                  <th className="th text-right">波动20</th>
                  <th className="th text-right">换手20</th>
                  <th className="th text-right">RSI14</th>
                </tr>
              </thead>
              <tbody>
                {screen.data.map((row) => (
                  <tr key={`${row.code}-${row.trade_date}`} className="border-t border-slate-800/60">
                    <td className="td">{row.code}</td>
                    <td className="td text-slate-400">{row.name}</td>
                    <td className="td text-right text-sky-300">{formatNumber(row.score, 3)}</td>
                    <td className={`td text-right ${changeColor(row.mom_20)}`}>
                      {formatPct(row.mom_20)}
                    </td>
                    <td className={`td text-right ${changeColor(row.mom_60)}`}>
                      {formatPct(row.mom_60)}
                    </td>
                    <td className="td text-right">{formatNumber(row.vol_20, 3)}</td>
                    <td className="td text-right">{formatNumber(row.turnover_20, 4)}</td>
                    <td className="td text-right">{formatNumber(row.rsi_14, 2)}</td>
                  </tr>
                ))}
                {screen.data.length === 0 && (
                  <tr>
                    <td className="td text-slate-500" colSpan={8}>
                      无匹配结果
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* Correlation */}
      <div className="card space-y-3">
        <div className="text-sm font-medium text-slate-300">相关性分析</div>
        <div className="flex flex-wrap items-end gap-2">
          <div className="min-w-0 flex-1 sm:min-w-[280px]">
            <label className="label">代码（逗号分隔，最多 30 个）</label>
            <input
              className="input w-full"
              value={correlationInput}
              onChange={(event) => setCorrelationInput(event.target.value)}
            />
          </div>
          <button
            className="btn btn-primary"
            onClick={() => correlation.mutate()}
            disabled={correlation.isPending}
          >
            {correlation.isPending ? "计算中…" : "计算相关性"}
          </button>
        </div>

        {correlation.isError && (
          <div className="text-sm text-amber-300">{(correlation.error as Error).message}</div>
        )}

        {heatmapOption && (
          <EChart option={heatmapOption} height={Math.max(320, 60 * correlation.data!.codes.length)} />
        )}
      </div>
    </div>
  );
}
