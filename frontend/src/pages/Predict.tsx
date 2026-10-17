import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { formatNumber } from "../format";
import type { EChartsOption } from "echarts";
import EChart from "../components/EChart";

const ALGOS = [
  { value: "hist_gbdt", label: "梯度提升 (HistGBDT)" },
  { value: "random_forest", label: "随机森林" },
  { value: "logistic", label: "逻辑回归 (基线)" },
];

export default function Predict() {
  const queryClient = useQueryClient();
  const [algo, setAlgo] = useState("hist_gbdt");
  const [horizon, setHorizon] = useState(1);
  const [detailCode, setDetailCode] = useState("");

  const models = useQuery({
    queryKey: ["models", horizon],
    queryFn: () => api.models(horizon),
  });

  const predictions = useQuery({
    queryKey: ["predictions", horizon],
    queryFn: () => api.predictions(horizon, 100),
  });

  const detail = useQuery({
    queryKey: ["predictionDetail", detailCode, horizon],
    queryFn: () => api.predictionDetail(detailCode, horizon),
    enabled: Boolean(detailCode),
    retry: false,
  });

  const train = useMutation({
    mutationFn: () => api.train({ algo, horizon }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["models"] });
    },
  });

  const runPredict = useMutation({
    mutationFn: () => api.predictRun(horizon),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["predictions"] }),
  });

  const active = models.data?.find((model) => model.is_active) ?? models.data?.[0];
  const metrics = (active?.metrics ?? {}) as Record<string, number | null | undefined>;

  const importanceOption: EChartsOption | null =
    active && Object.keys(active.importance ?? {}).length
      ? {
          backgroundColor: "transparent",
          grid: { left: 170, right: 30, top: 20, bottom: 30 },
          xAxis: { type: "value", axisLabel: { color: "#94a3b8" } },
          yAxis: {
            type: "category",
            data: Object.entries(active.importance)
              .sort((a, b) => a[1] - b[1])
              .slice(-15)
              .map(([key]) => key),
            axisLabel: { color: "#94a3b8" },
          },
          series: [
            {
              type: "bar",
              data: Object.entries(active.importance)
                .sort((a, b) => a[1] - b[1])
                .slice(-15)
                .map(([, value]) => value),
              itemStyle: { color: "#38bdf8" },
            },
          ],
        }
      : null;

  return (
    <div className="space-y-4">
      <div className="card space-y-3">
        <div className="text-sm font-medium text-slate-300">训练与预测</div>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="label">算法</label>
            <select className="input" value={algo} onChange={(e) => setAlgo(e.target.value)}>
              {ALGOS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label">预测周期（交易日）</label>
            <select
              className="input"
              value={horizon}
              onChange={(e) => setHorizon(Number(e.target.value))}
            >
              {[1, 5, 20].map((value) => (
                <option key={value} value={value}>
                  {value} 日
                </option>
              ))}
            </select>
          </div>
          <button className="btn" onClick={() => train.mutate()} disabled={train.isPending}>
            {train.isPending ? "训练中…" : "训练模型"}
          </button>
          <button
            className="btn btn-primary"
            onClick={() => runPredict.mutate()}
            disabled={runPredict.isPending}
          >
            {runPredict.isPending ? "预测中…" : "运行预测"}
          </button>
        </div>

        {train.isError && (
          <div className="text-sm text-amber-300">{(train.error as Error).message}</div>
        )}
        {runPredict.isError && (
          <div className="text-sm text-amber-300">{(runPredict.error as Error).message}</div>
        )}
        {runPredict.isSuccess && (
          <div className="text-sm text-emerald-300">
            已对 {runPredict.data.scored} 只标的完成打分
          </div>
        )}
      </div>

      {active && (
        <div className="card">
          <div className="mb-2 flex flex-wrap items-center gap-3 text-sm">
            <span className="font-medium text-slate-300">当前模型</span>
            <span className="text-slate-400">{active.model_id}</span>
            <span className="badge bg-slate-800 text-slate-300">{active.algo}</span>
            <span className="badge bg-slate-800 text-slate-300">{active.horizon} 日</span>
            {active.is_active && (
              <span className="badge bg-emerald-900/60 text-emerald-300">生效中</span>
            )}
          </div>

          <div className="grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-2 md:grid-cols-4">
            <Metric label="样本外 AUC" value={metrics.auc} digits={4} />
            <Metric label="AUC 基线" value={metrics.auc_baseline} digits={4} />
            <Metric label="准确率" value={metrics.accuracy} digits={4} />
            <Metric label="准确率基线" value={metrics.accuracy_baseline} digits={4} />
            <Metric label="IC (Spearman)" value={metrics.ic_spearman} digits={4} />
            <Metric label="IC (Pearson)" value={metrics.ic_pearson} digits={4} />
            <Metric label="分层收益差" value={metrics.decile_spread} digits={4} />
            <Metric label="训练样本" value={active.n_train} digits={0} />
            <Metric label="测试样本" value={active.n_test} digits={0} />
          </div>

          <p className="mt-3 rounded border border-amber-900/50 bg-amber-950/30 p-2 text-xs text-amber-300">
            模型指标应显式对比随机基线：AUC 需显著高于 0.5，IC 需显著非零，才说明存在有效信号。
            训练与测试按时间前后切分，并对标签重叠区间做了净化（purge），以避免未来信息泄漏。
          </p>

          <div className="mt-3 text-xs text-slate-500">
            训练区间 {String(active.train_start ?? "--")} ~ {String(active.train_end ?? "--")} ·
            测试区间 {String(active.test_start ?? "--")} ~ {String(active.test_end ?? "--")}
          </div>
        </div>
      )}

      {importanceOption && (
        <div className="card">
          <div className="mb-2 text-sm font-medium text-slate-300">特征重要性（Top 15）</div>
          <EChart option={importanceOption} height={420} />
        </div>
      )}

      <div className="card">
        <div className="mb-2 text-sm font-medium text-slate-300">
          预测结果{" "}
          <span className="text-xs text-slate-500">
            {predictions.data?.trade_date ? `数据日期 ${predictions.data.trade_date}` : ""}
          </span>
        </div>
        {predictions.data?.items?.length ? (
          <div className="table-wrap">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-800">
                  <th className="th">代码</th>
                  <th className="th">名称</th>
                  <th className="th text-right">上涨概率</th>
                  <th className="th text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {predictions.data.items.map((item) => (
                  <tr key={`${item.code}-${item.horizon}`} className="border-t border-slate-800/60">
                    <td className="td">{item.code}</td>
                    <td className="td text-slate-400">{item.name}</td>
                    <td className="td text-right">
                      <div className="flex items-center justify-end gap-2">
                        <div className="h-1.5 w-16 overflow-hidden rounded bg-slate-800 sm:w-24">
                          <div
                            className="h-full bg-sky-500"
                            style={{ width: `${(item.prob_up ?? 0) * 100}%` }}
                          />
                        </div>
                        <span className="w-14 text-right text-sky-300">
                          {item.prob_up === null ? "--" : `${(item.prob_up * 100).toFixed(1)}%`}
                        </span>
                      </div>
                    </td>
                    <td className="td text-right">
                      <button className="text-xs text-sky-400 hover:underline" onClick={() => setDetailCode(item.code)}>
                        查看
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-xs text-slate-500">暂无预测结果，请先训练并运行预测。</p>
        )}
      </div>

      {detailCode && (
        <div className="card">
          <div className="mb-2 flex items-center justify-between">
            <div className="text-sm font-medium text-slate-300">
              个股预测明细 · {detailCode}
            </div>
            <button className="btn" onClick={() => setDetailCode("")}>
              关闭
            </button>
          </div>
          {detail.isError && (
            <div className="text-sm text-amber-300">{(detail.error as Error).message}</div>
          )}
          {detail.data && (
            <div className="space-y-2 text-sm">
              <div className="text-slate-300">
                上涨概率{" "}
                <span className="text-sky-300">
                  {detail.data.prob_up === null
                    ? "--"
                    : `${(Number(detail.data.prob_up) * 100).toFixed(2)}%`}
                </span>
              </div>
              <div className="text-xs text-slate-500">{String(detail.data.disclaimer ?? "")}</div>
              {Array.isArray(detail.data.top_features) && (
                <div className="table-wrap">
                  <table className="w-full">
                    <thead>
                      <tr className="border-b border-slate-800">
                        <th className="th">特征</th>
                        <th className="th text-right">取值</th>
                        <th className="th text-right">重要性</th>
                      </tr>
                    </thead>
                    <tbody>
                      {(detail.data.top_features as Array<Record<string, number>>).map((row) => (
                        <tr key={String(row.feature)} className="border-t border-slate-800/60">
                          <td className="td text-slate-400">{String(row.feature)}</td>
                          <td className="td text-right">
                            {row.value === null ? "--" : formatNumber(row.value, 4)}
                          </td>
                          <td className="td text-right">
                            {row.importance === null ? "--" : formatNumber(row.importance, 4)}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function Metric({
  label,
  value,
  digits,
}: {
  label: string;
  value: number | null | undefined;
  digits: number;
}) {
  return (
    <div className="flex justify-between border-b border-slate-800/50 py-0.5">
      <span className="text-slate-400">{label}</span>
      <span className="text-slate-200">
        {value === null || value === undefined || Number.isNaN(Number(value))
          ? "--"
          : Number(value).toFixed(digits)}
      </span>
    </div>
  );
}
