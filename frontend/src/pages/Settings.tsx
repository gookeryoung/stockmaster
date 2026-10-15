import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { formatCompact } from "../format";

interface JobDef {
  key: string;
  label: string;
  hint: string;
  params?: Record<string, string | number | boolean>;
}

const CORE_JOBS: JobDef[] = [
  { key: "calendar", label: "同步交易日历", hint: "刷新未来一年的开市日" },
  { key: "instruments", label: "同步证券主数据", hint: "代码、名称、指数成分" },
  { key: "index_daily", label: "同步指数日线", hint: "沪深300、中证500、上证指数" },
  { key: "daily", label: "同步个股日线", hint: "按自选股/指数成分增量同步" },
  { key: "analytics", label: "计算指标与因子", hint: "写入指标表与因子表（含横截面标准化）" },
];

const AI_JOBS: JobDef[] = [
  { key: "train", label: "训练预测模型", hint: "默认梯度提升，1 日周期", params: { horizon: 1 } },
  { key: "predict", label: "运行预测", hint: "用生效模型对全市场打分", params: { horizon: 1 } },
  { key: "alerts", label: "扫描预警规则", hint: "评估全部启用规则（去重）" },
  { key: "snapshot", label: "拉取盘中快照", hint: "新浪快照，供盘中预警使用" },
  { key: "pipeline", label: "运行完整流水线", hint: "日历→主数据→日线→指标→预测→预警" },
];

export default function Settings() {
  const queryClient = useQueryClient();
  const [lastResult, setLastResult] = useState<string | null>(null);
  const [newWatchCode, setNewWatchCode] = useState("");

  const status = useQuery({ queryKey: ["status"], queryFn: api.status, refetchInterval: 20_000 });
  const watchlist = useQuery({ queryKey: ["watchlist"], queryFn: () => api.watchlist() });

  const runJob = useMutation({
    mutationFn: ({ key, params }: { key: string; params?: Record<string, string | number | boolean> }) =>
      api.runJob(key, params ?? {}),
    onSuccess: (data) => {
      setLastResult(`${data.job} 完成：${JSON.stringify(data.detail).slice(0, 400)}`);
      queryClient.invalidateQueries();
    },
    onError: (error) => setLastResult(`失败：${(error as Error).message}`),
  });

  const addWatch = useMutation({
    mutationFn: (code: string) => api.addWatch(code),
    onSuccess: () => {
      setNewWatchCode("");
      queryClient.invalidateQueries({ queryKey: ["watchlist"] });
    },
  });

  const removeWatch = useMutation({
    mutationFn: (id: number) => api.removeWatch(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
  });

  const data = status.data;

  return (
    <div className="space-y-4">
      {/* Jobs */}
      <div className="card space-y-3">
        <div className="text-sm font-medium text-slate-300">数据与计算任务</div>
        <p className="text-xs text-slate-500">
          任务为同步执行，完成后会返回结果。首次使用建议按顺序：交易日历 →
          证券主数据 → 日线 → 指标与因子。
        </p>

        <div className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
          {CORE_JOBS.map((job) => (
            <JobButton
              key={job.key}
              job={job}
              pending={runJob.isPending}
              onClick={() => runJob.mutate({ key: job.key, params: job.params })}
            />
          ))}
        </div>

        <div className="pt-2 text-sm font-medium text-slate-300">预测与预警</div>
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3">
          {AI_JOBS.map((job) => (
            <JobButton
              key={job.key}
              job={job}
              pending={runJob.isPending}
              onClick={() => runJob.mutate({ key: job.key, params: job.params })}
            />
          ))}
        </div>

        {lastResult && (
          <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded border border-slate-800 bg-slate-950 p-2 text-xs text-slate-300">
            {lastResult}
          </pre>
        )}
      </div>

      {/* Watchlist management */}
      <div className="card space-y-3">
        <div className="text-sm font-medium text-slate-300">自选股</div>
        <div className="flex items-center gap-2">
          <input
            className="input w-40"
            placeholder="如 600519.SH"
            value={newWatchCode}
            onChange={(event) => setNewWatchCode(event.target.value)}
          />
          <button
            className="btn"
            disabled={!newWatchCode || addWatch.isPending}
            onClick={() => addWatch.mutate(newWatchCode)}
          >
            添加
          </button>
          {addWatch.isError && (
            <span className="text-xs text-red-400">{(addWatch.error as Error).message}</span>
          )}
        </div>
        {watchlist.data?.length ? (
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-800">
                <th className="th">代码</th>
                <th className="th">名称</th>
                <th className="th text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {watchlist.data.map((item) => (
                <tr key={item.id} className="border-t border-slate-800/60">
                  <td className="td">{item.code}</td>
                  <td className="td text-slate-400">{item.name}</td>
                  <td className="td text-right">
                    <button
                      className="text-xs text-red-400 hover:underline"
                      onClick={() => removeWatch.mutate(item.id)}
                    >
                      移除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="text-xs text-slate-500">暂无自选股。</p>
        )}
      </div>

      {/* Status */}
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <div className="card space-y-2">
          <div className="text-sm font-medium text-slate-300">运行状态</div>
          {data ? (
            <div className="space-y-1 text-sm">
              <Row label="版本" value={data.version} />
              <Row label="时区" value={data.timezone} />
              <Row label="数据库" value={String(data.database.path ?? "")} />
              <Row label="数据库大小" value={`${data.database.size_mb ?? 0} MB`} />
              <Row label="日线行数" value={formatCompact(Number(data.data.bar_count ?? 0))} />
              <Row label="证券数量" value={String(data.data.instrument_count ?? 0)} />
              <Row label="交易日数量" value={String(data.data.calendar_days ?? 0)} />
              <Row label="最新行情日" value={String(data.data.latest_bar_date ?? "无")} />
              <Row
                label="调度器"
                value={data.scheduler.running ? "运行中" : "已停止"}
              />
            </div>
          ) : (
            <p className="text-xs text-slate-500">加载中…</p>
          )}
        </div>

        <div className="card space-y-2">
          <div className="text-sm font-medium text-slate-300">数据源状态</div>
          {data?.providers?.length ? (
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-800">
                  <th className="th">数据源</th>
                  <th className="th text-right">成功</th>
                  <th className="th text-right">失败</th>
                  <th className="th text-right">服务次数</th>
                  <th className="th">状态</th>
                </tr>
              </thead>
              <tbody>
                {data.providers.map((provider) => (
                  <tr key={provider.name} className="border-t border-slate-800/60">
                    <td className="td">{provider.name}</td>
                    <td className="td text-right">{provider.successes}</td>
                    <td className="td text-right">{provider.failures}</td>
                    <td className="td text-right">{provider.served}</td>
                    <td className="td">
                      <span
                        className={`badge ${
                          provider.circuit_open
                            ? "bg-red-900/60 text-red-300"
                            : "bg-emerald-900/55 text-emerald-300"
                        }`}
                      >
                        {provider.circuit_open ? "熔断" : "正常"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-xs text-slate-500">无数据源信息。</p>
          )}
          <p className="text-xs text-slate-500">
            主数据源为 baostock；当它超时或失败时，会自动降级到腾讯/新浪并在此显示熔断状态。
          </p>
        </div>
      </div>

      {data?.scheduler?.jobs?.length ? (
        <div className="card">
          <div className="mb-2 text-sm font-medium text-slate-300">定时任务</div>
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-800">
                <th className="th">任务</th>
                <th className="th">下次运行</th>
              </tr>
            </thead>
            <tbody>
              {data.scheduler.jobs.map((job) => (
                <tr key={job.id} className="border-t border-slate-800/60">
                  <td className="td">{job.id}</td>
                  <td className="td text-slate-400">{job.next_run ?? "--"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      <div className="card border-amber-900/50 bg-amber-950/20 text-xs text-amber-300">
        {data?.disclaimer ??
          "本工作台为量化研究工具，所有数据、指标、预测与回测结果仅供研究参考，不构成投资建议。"}
      </div>
    </div>
  );
}

function JobButton({
  job,
  pending,
  onClick,
}: {
  job: JobDef;
  pending: boolean;
  onClick: () => void;
}) {
  return (
    <button
      className="rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-left transition hover:border-sky-600 hover:bg-slate-800 disabled:opacity-40"
      disabled={pending}
      onClick={onClick}
    >
      <div className="text-sm text-slate-100">{job.label}</div>
      <div className="text-xs text-slate-500">{job.hint}</div>
    </button>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between border-b border-slate-800/50 py-0.5">
      <span className="text-slate-400">{label}</span>
      <span className="max-w-[60%] truncate text-slate-200" title={value}>
        {value}
      </span>
    </div>
  );
}
