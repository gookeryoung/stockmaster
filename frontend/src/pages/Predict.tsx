/** 预测页：把训练流水线拆成六个可解释、可自定义的模块。
 *
 * 设计取舍：
 * 1. 流水线（特征工程 → 标签构建 → 时序切分 → 模型训练 → 样本外评估 → 打分预测）
 *    逐段展示功能说明与可调项，而不是把算法/周期藏在两个下拉里。用户在页面内即时
 *    调整「特征子集 / 算法超参 / 测试集占比」，训练时随请求发往后端即时生效——
 *    不新增数据库表、不新增迁移，也不改变默认链路（不传即用后端默认）。
 * 2. 自定义草稿（特征勾选、超参、测试集占比）与 algo/horizon 一样存进 useViewState，
 *    刷新后原样恢复；训练结果、指标、预测列表属于可再生数据，交给 React Query。
 * 3. 「模块」的元数据（说明 / 特征分组 / 超参规格）集中在 predictModules.ts，与后端
 *    FEATURE_COLUMNS、PARAM_SPECS 对齐；提交前的范围校验只是提前反馈，最终以后端
 *    白名单为准。
 */

import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { EChartsOption } from "echarts";
import { api } from "../api";
import EChart from "../components/EChart";
import { formatNumber } from "../format";
import {
  ALGO_OPTIONS,
  ALGO_PARAM_SPECS,
  ALL_FEATURES,
  DEFAULT_ALGO_PARAMS,
  FEATURE_GROUPS,
  PIPELINE_MODULES,
} from "../predictModules";
import { useViewState } from "../view";

export default function Predict() {
  const queryClient = useQueryClient();
  const { algo, horizon, detailCode, features, params, testFraction } = useViewState(
    (state) => state.predict,
  );
  const setPredict = useViewState((state) => state.setPredict);
  const [validationError, setValidationError] = useState<string | null>(null);

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
    mutationFn: (payload: TrainPayload) => api.train(payload),
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

  const toggleFeature = (name: string) => {
    const next = features.includes(name)
      ? features.filter((item) => item !== name)
      : [...features, name];
    setPredict({ features: next });
  };

  const toggleGroup = (groupFeatures: string[]) => {
    const allSelected = groupFeatures.every((name) => features.includes(name));
    const next = allSelected
      ? features.filter((name) => !groupFeatures.includes(name))
      : Array.from(new Set([...features, ...groupFeatures]));
    setPredict({ features: next });
  };

  const changeAlgo = (next: string) => {
    // 超参名与算法强相关；切算法时替换为该算法的默认超参，避免把上一个算法的键带过去。
    setPredict({ algo: next, params: { ...DEFAULT_ALGO_PARAMS[next] } });
  };

  const setParam = (name: string, value: string) => {
    setPredict({ params: { ...params, [name]: value } });
  };

  const resetCustomization = () => {
    setValidationError(null);
    setPredict({
      algo: "hist_gbdt",
      features: [...ALL_FEATURES],
      params: { ...DEFAULT_ALGO_PARAMS.hist_gbdt },
      testFraction: "",
    });
  };

  const runTraining = () => {
    try {
      const payload = buildTrainPayload({ algo, horizon, features, params, testFraction });
      setValidationError(null);
      train.mutate(payload);
    } catch (error) {
      setValidationError(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <div className="space-y-4">
      <div className="card space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="text-sm font-medium text-slate-300">预测流水线（模块化）</div>
          <button className="btn" onClick={resetCustomization}>
            恢复默认
          </button>
        </div>
        <div className="flex flex-wrap items-center gap-1 text-xs">
          {PIPELINE_MODULES.map((module, index) => (
            <span key={module.id} className="flex items-center gap-1">
              {index > 0 && <span className="text-slate-600">→</span>}
              <span className="badge bg-slate-800 text-slate-300" title={module.desc}>
                <span className="mr-1 text-sky-400">{module.index}</span>
                {module.title}
              </span>
            </span>
          ))}
        </div>
        <p className="text-xs text-slate-500">
          下方按数据流顺序拆解各模块并标注功能说明与可调项；自定义在训练时随请求发往后端即时生效，
          无需另存配置。「恢复默认」可一键回到服务端默认链路。
        </p>
      </div>

      <ModuleCard module={0}>
        <div className="flex flex-wrap items-center gap-2 text-xs">
          <span className="text-slate-400">
            已选 <span className="text-sky-300">{features.length}</span> / {ALL_FEATURES.length} 个特征
          </span>
          <button className="btn px-2 py-0.5 text-xs" onClick={() => setPredict({ features: [...ALL_FEATURES] })}>
            全选
          </button>
          <button className="btn px-2 py-0.5 text-xs" onClick={() => setPredict({ features: [] })}>
            清空
          </button>
        </div>
        <div className="grid grid-cols-1 gap-2 md:grid-cols-2">
          {FEATURE_GROUPS.map((group) => {
            const groupFeatures = group.features.map((feature) => feature.name);
            const selectedCount = groupFeatures.filter((name) => features.includes(name)).length;
            const allSelected = selectedCount === groupFeatures.length;
            return (
              <div key={group.key} className="rounded-md border border-slate-800 bg-slate-950/40 p-2">
                <label className="flex items-center gap-2 text-xs">
                  <input
                    type="checkbox"
                    checked={allSelected}
                    ref={(node) => {
                      if (node) node.indeterminate = selectedCount > 0 && !allSelected;
                    }}
                    onChange={() => toggleGroup(groupFeatures)}
                  />
                  <span className="font-medium text-slate-300">{group.label}</span>
                  <span className="text-slate-500">
                    {selectedCount}/{groupFeatures.length}
                  </span>
                </label>
                <p className="mt-1 text-[11px] text-slate-500">{group.desc}</p>
                <div className="mt-2 flex flex-wrap gap-1">
                  {group.features.map((feature) => (
                    <label
                      key={feature.name}
                      className="flex items-center gap-1 rounded border border-slate-800 px-1.5 py-0.5 text-[11px] text-slate-400"
                      title={feature.name}
                    >
                      <input
                        type="checkbox"
                        checked={features.includes(feature.name)}
                        onChange={() => toggleFeature(feature.name)}
                      />
                      {feature.label}
                    </label>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
        {features.length === 0 && (
          <p className="text-xs text-amber-300">至少选择一个特征；否则训练请求会被拒绝。</p>
        )}
      </ModuleCard>

      <ModuleCard module={1}>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="label">预测周期 horizon（交易日）</label>
            <select
              className="input"
              value={horizon}
              onChange={(e) => setPredict({ horizon: Number(e.target.value) })}
            >
              {[1, 5, 20].map((value) => (
                <option key={value} value={value}>
                  {value} 日
                </option>
              ))}
            </select>
          </div>
          <p className="text-xs text-slate-500">
            标签为「{horizon} 个交易日后收盘价上涨」的二分类；周期越长，
            样本自相关越强，评估越需要对照基线。
          </p>
        </div>
      </ModuleCard>

      <ModuleCard module={2}>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="label">测试集占比 test_fraction</label>
            <input
              type="number"
              step="0.05"
              min={0}
              max={1}
              className="input w-32"
              value={testFraction}
              onChange={(e) => setPredict({ testFraction: e.target.value })}
              placeholder="留空=默认（0.2）"
            />
          </div>
          <p className="text-xs text-slate-500">
            按日历从后往前划出测试区间；训练尾部会净化 horizon 天，避免标签重叠导致泄漏。
          </p>
        </div>
      </ModuleCard>

      <ModuleCard module={3}>
        <div className="flex flex-wrap items-end gap-3">
          <div>
            <label className="label">算法 algo</label>
            <select className="input" value={algo} onChange={(e) => changeAlgo(e.target.value)}>
              {ALGO_OPTIONS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>
          <button className="btn btn-primary" onClick={runTraining} disabled={train.isPending}>
            {train.isPending ? "训练中…" : "训练模型"}
          </button>
        </div>

        <div className="mt-1 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {(ALGO_PARAM_SPECS[algo] ?? []).map((spec) => (
            <div key={spec.name}>
              <label className="label" title={spec.desc}>
                {spec.label}
              </label>
              <input
                type="number"
                className="input w-full"
                min={spec.min}
                max={spec.max}
                step={spec.step}
                value={params[spec.name] ?? ""}
                onChange={(e) => setParam(spec.name, e.target.value)}
                placeholder={`默认 ${spec.default}`}
              />
              <p className="mt-0.5 text-[11px] text-slate-500">
                [{spec.min}, {spec.max}] · 默认 {spec.default}
              </p>
            </div>
          ))}
        </div>
        <p className="text-xs text-slate-500">
          留空表示使用该算法的内置默认值；取值超出范围或类型不符时提交会被后端拒绝。
        </p>

        {(validationError || train.isError) && (
          <p className="text-xs text-amber-300">{validationError ?? (train.error as Error).message}</p>
        )}
        {train.isSuccess && <p className="text-xs text-emerald-300">已训练并注册新模型。</p>}
      </ModuleCard>

      <ModuleCard module={4}>
        {active ? (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-3 text-sm">
              <span className="text-slate-400">{active.model_id}</span>
              <span className="badge bg-slate-800 text-slate-300">{active.algo}</span>
              <span className="badge bg-slate-800 text-slate-300">{active.horizon} 日</span>
              <span className="badge bg-slate-800 text-slate-300">
                特征 {Object.keys(active.importance ?? {}).length} 个
              </span>
              {active.is_active && <span className="badge bg-emerald-900/60 text-emerald-300">生效中</span>}
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

            <p className="rounded border border-amber-900/50 bg-amber-950/30 p-2 text-xs text-amber-300">
              模型指标应显式对比随机基线：AUC 需显著高于 0.5，IC 需显著非零，才说明存在有效信号。
              训练与测试按时间前后切分，并对标签重叠区间做了净化（purge），以避免未来信息泄漏。
            </p>

            <div className="text-xs text-slate-500">
              训练区间 {String(active.train_start ?? "--")} ~ {String(active.train_end ?? "--")} · 测试区间{" "}
              {String(active.test_start ?? "--")} ~ {String(active.test_end ?? "--")}
            </div>

            {importanceOption && (
              <div>
                <div className="mb-1 text-xs font-medium text-slate-400">
                  特征重要性（Top 15，反映本模型实际使用了哪些输入）
                </div>
                <EChart option={importanceOption} height={380} />
              </div>
            )}
          </div>
        ) : (
          <p className="text-xs text-slate-500">尚无模型；先在上一步「模型训练」训练一个模型。</p>
        )}
      </ModuleCard>

      <ModuleCard module={5}>
        <div className="flex flex-wrap items-center gap-3">
          <button
            className="btn btn-primary"
            onClick={() => runPredict.mutate()}
            disabled={runPredict.isPending}
          >
            {runPredict.isPending ? "预测中…" : "运行预测"}
          </button>
          <span className="text-xs text-slate-500">
            {predictions.data?.trade_date ? `数据日期 ${predictions.data.trade_date}` : "尚无预测结果"}
          </span>
        </div>

        {runPredict.isError && (
          <p className="text-xs text-amber-300">{(runPredict.error as Error).message}</p>
        )}
        {runPredict.isSuccess && (
          <p className="text-xs text-emerald-300">已对 {runPredict.data.scored} 只标的完成打分</p>
        )}

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
                      <button
                        className="text-xs text-sky-400 hover:underline"
                        onClick={() => setPredict({ detailCode: item.code })}
                      >
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

        {detailCode && (
          <div className="space-y-2 rounded-md border border-slate-800 bg-slate-950/40 p-3">
            <div className="flex items-center justify-between">
              <div className="text-sm font-medium text-slate-300">个股预测明细 · {detailCode}</div>
              <button className="btn px-2 py-0.5 text-xs" onClick={() => setPredict({ detailCode: "" })}>
                关闭
              </button>
            </div>
            {detail.isError && (
              <p className="text-xs text-amber-300">{(detail.error as Error).message}</p>
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
      </ModuleCard>
    </div>
  );
}

interface TrainPayload {
  algo: string;
  horizon: number;
  features: string[];
  algo_params?: Record<string, number>;
  test_fraction?: number;
}

/** 组装训练请求，并把前端能提前发现的问题（空特征、越界超参等）就地拦下。 */
function buildTrainPayload(input: {
  algo: string;
  horizon: number;
  features: string[];
  params: Record<string, string>;
  testFraction: string;
}): TrainPayload {
  if (input.features.length === 0) throw new Error("请至少选择一个特征");
  const selected = new Set(input.features);
  const features = ALL_FEATURES.filter((name) => selected.has(name));

  let test_fraction: number | undefined;
  const fractionDraft = input.testFraction.trim();
  if (fractionDraft !== "") {
    const fraction = Number(fractionDraft);
    if (!Number.isFinite(fraction) || fraction <= 0 || fraction >= 1) {
      throw new Error("测试集占比需在 (0, 1) 之间");
    }
    test_fraction = fraction;
  }

  const algo_params: Record<string, number> = {};
  (ALGO_PARAM_SPECS[input.algo] ?? []).forEach((spec) => {
    const raw = (input.params[spec.name] ?? "").trim();
    if (raw === "") return;
    const value = Number(raw);
    if (!Number.isFinite(value)) throw new Error(`超参 ${spec.label} 必须是数字`);
    if (spec.kind === "int" && !Number.isInteger(value)) {
      throw new Error(`超参 ${spec.label} 必须是整数`);
    }
    if (value < spec.min || value > spec.max) {
      throw new Error(`超参 ${spec.label} 应在 [${spec.min}, ${spec.max}] 内`);
    }
    algo_params[spec.name] = value;
  });

  const payload: TrainPayload = { algo: input.algo, horizon: input.horizon, features };
  if (test_fraction !== undefined) payload.test_fraction = test_fraction;
  if (Object.keys(algo_params).length > 0) payload.algo_params = algo_params;
  return payload;
}

/** 单个流水线模块：编号 + 标题 + 功能说明 + 可调项。 */
function ModuleCard({ module, children }: { module: number; children: ReactNode }) {
  const meta = PIPELINE_MODULES[module];
  return (
    <div className="card space-y-3">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="flex h-6 w-6 items-center justify-center rounded-full bg-sky-500/15 text-xs font-medium text-sky-300">
          {meta.index}
        </span>
        <div className="text-sm font-medium text-slate-200">{meta.title}</div>
      </div>
      <p className="text-xs text-slate-500">{meta.desc}</p>
      {children}
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
