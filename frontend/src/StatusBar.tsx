import { useQuery } from "@tanstack/react-query";
import { api } from "./api";
import { formatCompact } from "./format";

export default function StatusBar() {
  const { data, isError, error } = useQuery({
    queryKey: ["status"],
    queryFn: api.status,
    refetchInterval: 30_000,
  });

  if (isError) {
    return (
      <div className="border-b border-amber-900/60 bg-amber-950/40 px-4 py-1.5 text-xs text-amber-300">
        无法连接后端 API：{(error as Error).message}
      </div>
    );
  }
  if (!data) return null;

  const degraded = data.providers.filter((p) => p.circuit_open);
  const barCount = Number(data.data.bar_count ?? 0);
  const latest = String(data.data.latest_bar_date ?? "无数据");
  const schedulerRunning = data.scheduler.running;

  return (
    <div className="flex flex-wrap items-center gap-x-5 gap-y-1 border-b border-slate-800 bg-slate-950/80 px-4 py-1.5 text-xs text-slate-400">
      <span>
        版本 <span className="text-slate-200">{data.version}</span>
      </span>
      <span>
        日线数据 <span className="text-slate-200">{formatCompact(barCount)}</span> 行 · 最新{" "}
        <span className="text-slate-200">{latest}</span>
      </span>
      <span>
        调度器{" "}
        <span className={schedulerRunning ? "text-emerald-400" : "text-slate-500"}>
          {schedulerRunning ? "运行中" : "已停止"}
        </span>
      </span>
      <span>
        数据源{" "}
        {data.providers.map((p) => (
          <span
            key={p.name}
            title={p.last_error || `${p.successes} 成功 / ${p.failures} 失败`}
            className={`badge mr-1 ${
              p.circuit_open
                ? "bg-red-900/60 text-red-300"
                : "bg-emerald-900/50 text-emerald-300"
            }`}
          >
            {p.name}
            {p.circuit_open ? " 熔断" : ""}
          </span>
        ))}
      </span>
      {degraded.length > 0 && (
        <span className="text-amber-400">已降级至备用数据源</span>
      )}
    </div>
  );
}
