/** 用量与配额：8 类资源表 + 合计、30 天趋势（图 + 数据表替代）、CSV 导出、
 * 以及工作区预警策略（独立卡片）。
 *
 * 用量只读，所有成员可见；配额默认不限（limits=0），真正拦截在服务端写路径。
 */

import { useMemo } from "react";
import { useMutation } from "@tanstack/react-query";
import type { EChartsOption } from "echarts";
import { api } from "../../api";
import EChart from "../../components/EChart";
import type { Workspace, WorkspaceUsage, WorkspaceUsagePoint } from "../../types";
import AlertPolicySection from "./AlertPolicySection";

function UsageTable({ data }: { data: WorkspaceUsage }) {
  return (
    <>
      <div className="table-wrap">
        <table className="w-full">
          <thead>
            <tr className="border-b border-slate-800">
              <th className="th">资源</th>
              <th className="th text-right">用量</th>
              <th className="th text-right">上限</th>
              <th className="th">状态</th>
            </tr>
          </thead>
          <tbody>
            {Object.keys(data.usage).length === 0 && (
              <tr>
                <td className="td text-slate-500" colSpan={4}>
                  暂无用量数据。
                </td>
              </tr>
            )}
            {Object.entries(data.usage).map(([key, used]) => {
              const limit = data.limits[key] ?? 0;
              const atLimit = limit > 0 && used >= limit;
              return (
                <tr key={key} className="border-b border-slate-800/50 last:border-0">
                  <td className="td text-slate-300">{data.labels[key] ?? key}</td>
                  <td className="td text-right text-slate-300">{used}</td>
                  <td className="td text-right text-slate-400">{limit > 0 ? limit : "不限"}</td>
                  <td className="td">
                    {atLimit ? (
                      <span className="badge bg-rose-900/50 text-rose-300">已达上限</span>
                    ) : (
                      <span className="badge bg-slate-800 text-slate-400">正常</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <p className="mt-2 text-xs text-slate-500">
        合计 {data.total} 项用量（含成员与通道） · 配额由管理员通过 SM_WORKSPACE_MAX_* 配置，默认不限；
        达到上限后相关写入会被服务端拒绝（409）；隔离关闭时不生效。
      </p>
    </>
  );
}

interface Props {
  current: Workspace;
  usage: WorkspaceUsage | undefined;
  usageLoading: boolean;
  usageError: boolean;
  onRetryUsage: () => void;
  history: WorkspaceUsagePoint[] | undefined;
  historyLoading: boolean;
  historyError: boolean;
  onRetryHistory: () => void;
  canManage: boolean;
  adminViewing?: boolean;
  onFlash: (text: string) => void;
}

export default function UsageSection({
  current,
  usage,
  usageLoading,
  usageError,
  onRetryUsage,
  history,
  historyLoading,
  historyError,
  onRetryHistory,
  canManage,
  adminViewing = false,
  onFlash,
}: Props) {
  const exportUsage = useMutation({
    mutationFn: () => api.downloadWorkspaceUsage(current.id),
    onError: (error) => onFlash(`导出用量失败：${(error as Error).message}`),
  });

  // 少于 2 个点时降级为提示文案，不画空图。
  const historyOption = useMemo<EChartsOption | null>(() => {
    const points = history ?? [];
    if (points.length < 2) return null;
    return {
      backgroundColor: "transparent",
      tooltip: { trigger: "axis" },
      legend: { data: ["数据量", "成员数"], textStyle: { color: "#94a3b8" } },
      grid: { left: 48, right: 20, top: 32, bottom: 28 },
      xAxis: {
        type: "category",
        data: points.map((point) => point.captured_on),
        axisLabel: { color: "#94a3b8" },
        axisLine: { lineStyle: { color: "#334155" } },
      },
      yAxis: {
        type: "value",
        minInterval: 1,
        splitLine: { lineStyle: { color: "#1e293b" } },
        axisLabel: { color: "#94a3b8" },
      },
      series: [
        {
          name: "数据量",
          type: "line",
          data: points.map((point) => point.data_total),
          showSymbol: false,
          smooth: true,
          lineStyle: { color: "#38bdf8", width: 1.6 },
          areaStyle: { color: "rgba(56,189,248,0.12)" },
        },
        {
          name: "成员数",
          type: "line",
          data: points.map((point) => point.member_count),
          showSymbol: false,
          smooth: true,
          lineStyle: { color: "#f59e0b", width: 1.4 },
        },
      ],
    };
  }, [history]);

  const points = history ?? [];

  return (
    <section id="usage" className="scroll-mt-16 space-y-3">
      <div className="card">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-100">用量与配额 · {current.name}</h2>
          <div className="flex items-center gap-2">
            <span className="text-xs text-slate-500">只读 · 达到上限时相关写入会被服务端拒绝</span>
            <button
              className="btn px-2 py-1 text-xs"
              disabled={exportUsage.isPending}
              onClick={() => exportUsage.mutate()}
              title="导出当前工作区的用量明细 CSV"
            >
              {exportUsage.isPending ? "导出中…" : "导出 CSV"}
            </button>
          </div>
        </div>

        {usageLoading && <p className="text-xs text-slate-500">用量加载中…</p>}
        {!usageLoading && usageError && (
          <p className="text-xs text-amber-300">
            用量信息暂不可用，不影响页面其他操作{" "}
            <button className="underline" type="button" onClick={onRetryUsage}>
              重试
            </button>
          </p>
        )}
        {!usageLoading && !usageError && usage && <UsageTable data={usage} />}

        <div className="mt-3 border-t border-slate-800 pt-3">
          <div className="mb-1 flex items-baseline justify-between">
            <span className="text-xs font-medium text-slate-300">近 30 天趋势</span>
            <span className="text-[11px] text-slate-500">数据量 / 成员数</span>
          </div>
          {historyLoading && <p className="text-xs text-slate-500">趋势加载中…</p>}
          {!historyLoading && historyError && (
            <p className="text-xs text-amber-300">
              趋势数据暂不可用{" "}
              <button className="underline" type="button" onClick={onRetryHistory}>
                重试
              </button>
            </p>
          )}
          {!historyLoading && !historyError && historyOption && (
            <>
              <div role="img" aria-label="近 30 天数据量与成员数趋势折线图">
                <EChart option={historyOption} height={180} />
              </div>
              <details className="mt-1">
                <summary className="cursor-pointer text-xs text-slate-400 hover:text-slate-300">
                  查看数据表（趋势的表格替代）
                </summary>
                <div className="table-wrap mt-1">
                  <table className="w-full">
                    <thead>
                      <tr className="border-b border-slate-800">
                        <th className="th">日期</th>
                        <th className="th text-right">数据量</th>
                        <th className="th text-right">成员</th>
                        <th className="th text-right">自选</th>
                        <th className="th text-right">规则</th>
                        <th className="th text-right">回测</th>
                        <th className="th text-right">配置合计</th>
                      </tr>
                    </thead>
                    <tbody>
                      {points.map((point) => (
                        <tr key={point.captured_on} className="border-b border-slate-800/50 last:border-0">
                          <td className="td text-xs text-slate-300">{point.captured_on}</td>
                          <td className="td text-right text-slate-300">{point.data_total}</td>
                          <td className="td text-right text-slate-300">{point.member_count}</td>
                          <td className="td text-right text-slate-300">{point.watchlist_items}</td>
                          <td className="td text-right text-slate-300">{point.alert_rules}</td>
                          <td className="td text-right text-slate-300">{point.backtest_runs}</td>
                          <td className="td text-right text-slate-300">{point.config_total}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </details>
            </>
          )}
          {!historyLoading && !historyError && !historyOption && (
            <p className="text-xs text-slate-500">趋势数据从每日快照开始积累（每日 17:30）。</p>
          )}
        </div>
      </div>

      <AlertPolicySection
        workspaceId={current.id}
        canManage={canManage}
        adminViewing={adminViewing}
        onFlash={onFlash}
      />
    </section>
  );
}
