/** 策略配置页：工作区级的「选股 / 样本 / 算法」三份可复用命名配置。
 *
 * 设计取舍：
 * 1. 三份配置放在同一页面的三个 tab 里，因为它们在业务上是一条流水线
 *    （选股条件 -> 训练样本 -> 算法参数），放一起可以减少来回跳页；
 *    tab 本身也是操作状态，存进 useViewState，刷新后仍停在原来的 tab。
 * 2. 表单草稿（字符串形式）放 useViewState 持久化；服务端数据（配置列表）交给
 *    React Query；运行/解析/训练结果属于「执行一次就能再生」的派生物，用组件本地
 *    useState 保存，切走 tab 即丢弃，避免把易陈旧的结果写进本地存储。
 * 3. 选股条件用逐行文本「field op value」维护，而不是 JSON 输入框或半屏高的
 *    结构化编辑器；提交前解析，无法解析的行给出带行号的提示，不静默丢弃。
 * 4. viewer 角色下所有写操作（新建/编辑/保存/删除/训练）禁用；运行选股与解析样本
 *    是只读求值，仍然可用（与后端权限矩阵一致）。
 * 5. 编辑已有项时把整行数据填回表单，PATCH 仍提交完整 input，避免后端 replace
 *    语义下漏字段被重置。
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { formatDateTime, formatNumber } from "../format";
import { canWriteWorkspace, useCurrentWorkspace } from "../workspace";
import { useViewState } from "../view";
import type {
  AlgorithmConfigForm,
  SampleConfigForm,
  ScreeningConfigForm,
  StrategyTab,
} from "../view";
import type {
  AlgorithmConfig,
  AlgorithmConfigInput,
  AlgorithmTrainResult,
  SampleConfig,
  SampleConfigInput,
  SampleResolveResult,
  SampleUniverseType,
  ScreeningCondition,
  ScreeningConfig,
  ScreeningConfigInput,
  ScreeningOp,
  ScreeningParams,
  ScreeningRunResult,
  ScreenRow,
} from "../types";

const TABS: Array<{ value: StrategyTab; label: string; hint: string }> = [
  {
    value: "screening",
    label: "选股配置",
    hint: "保存可复用的因子选股条件；「运行」按当前数据即时求值，不会修改任何数据。",
  },
  {
    value: "sample",
    label: "样本配置",
    hint: "定义训练样本的标的来源、时间区间与持有期；「解析」可预览最终代码集合。",
  },
  {
    value: "algorithm",
    label: "算法配置",
    hint: "组合算法与样本配置的超参数；「训练」会生成新模型并写入模型列表。",
  },
];

const ALGO_OPTIONS = [
  { value: "hist_gbdt", label: "HistGradientBoosting（梯度提升树）" },
  { value: "random_forest", label: "随机森林" },
  { value: "logistic", label: "逻辑回归" },
];

/** 切换算法时的默认超参，避免把上一算法的参数名带过去。 */
const ALGO_PARAMS: Record<string, Record<string, string>> = {
  hist_gbdt: { max_iter: "300", learning_rate: "0.05", max_depth: "4", min_samples_leaf: "50" },
  random_forest: { n_estimators: "200", max_depth: "8", min_samples_leaf: "20" },
  logistic: { max_iter: "500", C: "1.0" },
};

const UNIVERSE_LABEL: Record<SampleUniverseType, string> = {
  watchlist: "自选清单",
  screening: "选股配置",
  codes: "指定代码",
  default: "默认宇宙",
};

export default function StrategyConfigs() {
  const tab = useViewState((state) => state.strategy.tab);
  const setStrategy = useViewState((state) => state.setStrategy);
  const readOnly = !canWriteWorkspace(useCurrentWorkspace());
  const active = TABS.find((item) => item.value === tab) ?? TABS[0];

  return (
    <div className="space-y-4">
      <div className="card space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          {TABS.map((item) => (
            <button
              key={item.value}
              className={`btn ${tab === item.value ? "btn-primary" : ""}`}
              onClick={() => setStrategy({ tab: item.value })}
            >
              {item.label}
            </button>
          ))}
        </div>
        <p className="text-xs text-slate-500">{active.hint}</p>
        {readOnly && (
          <p className="text-xs text-amber-300">
            当前工作区为只读角色（viewer）：配置不可新建、修改、删除或训练，仅可查看列表、运行选股与解析样本。
          </p>
        )}
      </div>

      {tab === "screening" && <ScreeningPanel />}
      {tab === "sample" && <SamplePanel />}
      {tab === "algorithm" && <AlgorithmPanel />}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 选股配置
// ---------------------------------------------------------------------------

function ScreeningPanel() {
  const queryClient = useQueryClient();
  const workspace = useCurrentWorkspace();
  const readOnly = !canWriteWorkspace(workspace);
  const { screening: form } = useViewState((state) => state.strategy);
  const setScreeningForm = useViewState((state) => state.setScreeningForm);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [conditionErrors, setConditionErrors] = useState<string[]>([]);
  const [runResult, setRunResult] = useState<{ name: string; data: ScreeningRunResult } | null>(
    null,
  );

  const configs = useQuery({
    queryKey: ["strategy", "screenings", workspace?.id],
    queryFn: api.screeningConfigs,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["strategy", "screenings"] });

  const saveConfig = useMutation({
    mutationFn: (vars: { id: number | null; input: ScreeningConfigInput }) =>
      vars.id === null
        ? api.createScreeningConfig(vars.input)
        : api.updateScreeningConfig(vars.id, vars.input),
    onSuccess: (_data, vars) => {
      setNotice({ kind: "ok", text: vars.id === null ? "选股配置已创建" : "选股配置已保存" });
      invalidate();
    },
    onError: (error) => setNotice({ kind: "error", text: errorText(error) }),
  });

  const removeConfig = useMutation({
    mutationFn: (id: number) => api.deleteScreeningConfig(id),
    onSuccess: (_data, id) => {
      setNotice({ kind: "ok", text: "选股配置已删除" });
      setEditingId((current) => (current === id ? null : current));
      invalidate();
    },
    onError: (error) => setNotice({ kind: "error", text: errorText(error) }),
  });

  const runConfig = useMutation({
    mutationFn: (vars: { id: number; name: string }) => api.runScreeningConfig(vars.id),
    onSuccess: (data, vars) => {
      setRunResult({ name: vars.name, data });
      setNotice({
        kind: "ok",
        text: `选股运行完成：命中 ${data.count} 只（交易日 ${data.trade_date ?? "未知"}）`,
      });
    },
    onError: (error) => setNotice({ kind: "error", text: errorText(error) }),
  });

  const resetFields = () => {
    setScreeningForm({
      name: "",
      description: "",
      min_score: "",
      max_vol: "",
      min_amount_log: "",
      exclude_st: true,
      boards: "",
      conditions: "",
      order_by: "score",
      direction: "desc",
      limit: 50,
    });
    setEditingId(null);
    setConditionErrors([]);
  };

  const startCreate = () => {
    resetFields();
    setNotice(null);
  };

  const startEdit = (config: ScreeningConfig) => {
    fillScreeningForm(config, setScreeningForm);
    setEditingId(config.id);
    setConditionErrors([]);
    setNotice({ kind: "ok", text: `已把「${config.name}」载入表单` });
  };

  const submit = () => {
    const errors: string[] = [];
    if (!form.name.trim()) errors.push("请填写配置名称");
    const numericFields: Array<[string, string]> = [
      ["最低评分", form.min_score],
      ["最大波动率", form.max_vol],
      ["最低成交额对数", form.min_amount_log],
    ];
    numericFields.forEach(([label, raw]) => {
      if (!isBlankOrNumber(raw)) errors.push(`${label}必须填数字或留空`);
    });
    if (!Number.isFinite(form.limit) || form.limit <= 0) errors.push("结果上限必须是正整数");
    const parsed = parseConditions(form.conditions);
    setConditionErrors(parsed.errors);
    if (parsed.errors.length > 0) errors.push("自定义条件存在无法解析的行");
    if (errors.length > 0) {
      setNotice({ kind: "error", text: errors.join("；") });
      return;
    }

    const params: ScreeningParams = {
      min_score: optionalNumber(form.min_score),
      max_vol: optionalNumber(form.max_vol),
      min_amount_log: optionalNumber(form.min_amount_log),
      exclude_st: form.exclude_st,
      boards: splitBoards(form.boards),
      conditions: parsed.conditions,
      order_by: form.order_by.trim() || "score",
      direction: form.direction,
      limit: Math.trunc(form.limit),
    };
    setNotice(null);
    saveConfig.mutate({
      id: editingId,
      input: { name: form.name.trim(), description: form.description.trim(), params },
    });
  };

  return (
    <div className="space-y-4">
      {notice && <NoticeLine notice={notice} />}

      {/* 编辑表单 */}
      <div className="card space-y-3">
        <div className="text-sm font-medium text-slate-300">
          {editingId === null ? "新建选股配置" : `编辑选股配置 #${editingId}`}
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-4">
          <div className="sm:col-span-2">
            <label className="label">配置名称</label>
            <input
              className="input w-full"
              value={form.name}
              onChange={(event) => setScreeningForm({ name: event.target.value })}
              placeholder="例如：低波动高股息"
            />
          </div>
          <div className="sm:col-span-2">
            <label className="label">说明（可选）</label>
            <input
              className="input w-full"
              value={form.description}
              onChange={(event) => setScreeningForm({ description: event.target.value })}
            />
          </div>
          <div>
            <label className="label">最低综合评分 min_score</label>
            <input
              type="number"
              step="0.1"
              className="input w-full"
              value={form.min_score}
              onChange={(event) => setScreeningForm({ min_score: event.target.value })}
              placeholder="留空=不限"
            />
          </div>
          <div>
            <label className="label">最大波动率 max_vol</label>
            <input
              type="number"
              step="0.01"
              className="input w-full"
              value={form.max_vol}
              onChange={(event) => setScreeningForm({ max_vol: event.target.value })}
              placeholder="留空=不限"
            />
          </div>
          <div>
            <label className="label">最低成交额对数 min_amount_log</label>
            <input
              type="number"
              step="0.1"
              className="input w-full"
              value={form.min_amount_log}
              onChange={(event) => setScreeningForm({ min_amount_log: event.target.value })}
              placeholder="留空=不限"
            />
          </div>
          <div>
            <label className="label">结果上限 limit</label>
            <input
              type="number"
              min={1}
              className="input w-full"
              value={form.limit}
              onChange={(event) => setScreeningForm({ limit: Number(event.target.value) })}
            />
          </div>
          <div>
            <label className="label">排序字段 order_by</label>
            <input
              className="input w-full"
              value={form.order_by}
              onChange={(event) => setScreeningForm({ order_by: event.target.value })}
              placeholder="score"
            />
          </div>
          <div>
            <label className="label">排序方向</label>
            <select
              className="input w-full"
              value={form.direction}
              onChange={(event) =>
                setScreeningForm({ direction: event.target.value as "asc" | "desc" })
              }
            >
              <option value="desc">降序</option>
              <option value="asc">升序</option>
            </select>
          </div>
          <div className="sm:col-span-2">
            <label className="label">板块 boards（逗号或空格分隔）</label>
            <input
              className="input w-full"
              value={form.boards}
              onChange={(event) => setScreeningForm({ boards: event.target.value })}
              placeholder="main, gem, star（留空=全部）"
            />
          </div>
          <div className="flex items-end">
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={form.exclude_st}
                onChange={(event) => setScreeningForm({ exclude_st: event.target.checked })}
              />
              排除 ST
            </label>
          </div>
          <div className="sm:col-span-2 md:col-span-4">
            <label className="label">自定义条件（每行一条 field op value，# 开头为注释）</label>
            <textarea
              rows={3}
              className="input w-full font-mono"
              value={form.conditions}
              onChange={(event) => {
                setScreeningForm({ conditions: event.target.value });
                if (conditionErrors.length > 0) setConditionErrors([]);
              }}
              placeholder={"mom_20>=0.5\nrsi_14<=70"}
            />
            {conditionErrors.length > 0 && (
              <ul className="mt-1 space-y-0.5 text-xs text-amber-300">
                {conditionErrors.map((message, index) => (
                  <li key={`${index}-${message}`}>{message}</li>
                ))}
              </ul>
            )}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            className="btn btn-primary"
            onClick={submit}
            disabled={saveConfig.isPending || readOnly}
          >
            {saveConfig.isPending ? "保存中…" : editingId === null ? "创建配置" : "保存修改"}
          </button>
          <button className="btn" onClick={startCreate} disabled={readOnly}>
            {editingId === null ? "重置表单" : "取消编辑"}
          </button>
          <span className="text-xs text-slate-500">
            条件支持 &gt;= &lt;= &gt; &lt; ==，例如 mom_20&gt;=0.5；留空表示不加自定义条件。
          </span>
        </div>
      </div>

      {/* 配置列表 */}
      <div className="card">
        <div className="mb-2 text-sm font-medium text-slate-300">选股配置列表</div>
        {configs.isPending && <p className="text-xs text-slate-500">加载中…</p>}
        {configs.isError && <p className="text-xs text-amber-300">{errorText(configs.error)}</p>}
        {configs.data && configs.data.length === 0 && (
          <p className="text-xs text-slate-500">尚未创建选股配置。</p>
        )}
        {configs.data && configs.data.length > 0 && (
          <div className="table-wrap">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-800">
                  <th className="th">名称</th>
                  <th className="th">参数摘要</th>
                  <th className="th hidden md:table-cell">更新时间</th>
                  <th className="th text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {configs.data.map((config) => {
                  const running = runConfig.isPending && runConfig.variables?.id === config.id;
                  return (
                    <tr key={config.id} className="border-t border-slate-800/60">
                      <td className="td">
                        {config.name}
                        {config.description && (
                          <span className="ml-1 hidden text-xs text-slate-500 sm:inline">
                            {config.description}
                          </span>
                        )}
                      </td>
                      <td
                        className="td max-w-[30rem] whitespace-normal text-xs text-slate-400"
                        title={conditionTitle(config.params)}
                      >
                        {screeningSummary(config.params)}
                      </td>
                      <td className="td hidden text-xs text-slate-500 md:table-cell">
                        {formatDateTime(config.updated_at)}
                      </td>
                      <td className="td text-right">
                        <button
                          className="mr-2 text-xs text-sky-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
                          disabled={running}
                          onClick={() => runConfig.mutate({ id: config.id, name: config.name })}
                        >
                          {running ? "运行中…" : "运行"}
                        </button>
                        <button
                          className="mr-2 text-xs text-sky-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
                          disabled={readOnly}
                          onClick={() => startEdit(config)}
                        >
                          编辑
                        </button>
                        <button
                          className="text-xs text-red-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
                          disabled={readOnly || removeConfig.isPending}
                          onClick={() => {
                            if (!window.confirm(`确认删除选股配置「${config.name}」？`)) return;
                            removeConfig.mutate(config.id);
                          }}
                        >
                          删除
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 运行结果（本地派生数据，切走即丢弃） */}
      {runResult && (
        <div className="card">
          <div className="mb-2 text-sm font-medium text-slate-300">
            运行结果{" "}
            <span className="text-xs text-slate-500">
              「{runResult.name}」 · 交易日 {runResult.data.trade_date ?? "--"} · 命中{" "}
              {runResult.data.count} 只
            </span>
          </div>
          {runResult.data.items.length === 0 ? (
            <p className="text-xs text-slate-500">当前数据下没有命中的标的。</p>
          ) : (
            <div className="table-wrap">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-slate-800">
                    <th className="th">代码</th>
                    <th className="th">名称</th>
                    <th className="th text-right">评分</th>
                    <th className="th text-right">mom_20</th>
                    <th className="th text-right">vol_20</th>
                    <th className="th text-right">turnover_20</th>
                    <th className="th text-right">rsi_14</th>
                  </tr>
                </thead>
                <tbody>
                  {runResult.data.items.map((row) => (
                    <ScreeningResultRow key={row.code} row={row} />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ScreeningResultRow({ row }: { row: ScreenRow }) {
  return (
    <tr className="border-t border-slate-800/60">
      <td className="td">{row.code}</td>
      <td className="td text-slate-300">{row.name}</td>
      <td className="td text-right">{formatNumber(row.score, 2)}</td>
      <td className="td text-right">{formatNumber(row.mom_20, 3)}</td>
      <td className="td text-right">{formatNumber(row.vol_20, 3)}</td>
      <td className="td text-right">{formatNumber(row.turnover_20, 3)}</td>
      <td className="td text-right">{formatNumber(row.rsi_14, 2)}</td>
    </tr>
  );
}

// ---------------------------------------------------------------------------
// 样本配置
// ---------------------------------------------------------------------------

function SamplePanel() {
  const queryClient = useQueryClient();
  const workspace = useCurrentWorkspace();
  const readOnly = !canWriteWorkspace(workspace);
  const { sample: form } = useViewState((state) => state.strategy);
  const setSampleForm = useViewState((state) => state.setSampleForm);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [resolveResult, setResolveResult] = useState<{
    name: string;
    data: SampleResolveResult;
  } | null>(null);

  const configs = useQuery({
    queryKey: ["strategy", "samples", workspace?.id],
    queryFn: api.sampleConfigs,
  });
  // 供 universe_type=screening 时选择引用的选股配置。
  const screenings = useQuery({
    queryKey: ["strategy", "screenings", workspace?.id],
    queryFn: api.screeningConfigs,
  });

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["strategy", "samples"] });

  const saveConfig = useMutation({
    mutationFn: (vars: { id: number | null; input: SampleConfigInput }) =>
      vars.id === null
        ? api.createSampleConfig(vars.input)
        : api.updateSampleConfig(vars.id, vars.input),
    onSuccess: (_data, vars) => {
      setNotice({ kind: "ok", text: vars.id === null ? "样本配置已创建" : "样本配置已保存" });
      invalidate();
    },
    onError: (error) => setNotice({ kind: "error", text: errorText(error) }),
  });

  const removeConfig = useMutation({
    mutationFn: (id: number) => api.deleteSampleConfig(id),
    onSuccess: (_data, id) => {
      setNotice({ kind: "ok", text: "样本配置已删除" });
      setEditingId((current) => (current === id ? null : current));
      invalidate();
    },
    onError: (error) => setNotice({ kind: "error", text: errorText(error) }),
  });

  const resolveConfig = useMutation({
    mutationFn: (vars: { id: number; name: string }) => api.resolveSampleConfig(vars.id),
    onSuccess: (data, vars) => {
      setResolveResult({ name: vars.name, data });
      setNotice({ kind: "ok", text: `样本解析完成：${data.count} 只标的` });
    },
    onError: (error) => setNotice({ kind: "error", text: errorText(error) }),
  });

  const resetFields = () => {
    setSampleForm({
      name: "",
      description: "",
      universe_type: "watchlist",
      universe_value: "default",
      start: "",
      end: "",
      lookback_days: 730,
      horizon: 5,
      min_bars: 60,
    });
    setEditingId(null);
  };

  const startCreate = () => {
    resetFields();
    setNotice(null);
  };

  const startEdit = (config: SampleConfig) => {
    fillSampleForm(config, setSampleForm);
    setEditingId(config.id);
    setNotice({ kind: "ok", text: `已把「${config.name}」载入表单` });
  };

  const changeUniverseType = (next: SampleUniverseType) => {
    // 切换来源时给一个合理的默认值，避免把上一种来源的取值误当成当前来源的值。
    let value = "";
    if (next === "watchlist") value = "default";
    else if (next === "screening" && screenings.data?.length) {
      value = String(screenings.data[0].id);
    }
    setSampleForm({ universe_type: next, universe_value: value });
  };

  const submit = () => {
    const errors: string[] = [];
    if (!form.name.trim()) errors.push("请填写配置名称");

    let universeValue = "";
    if (form.universe_type === "watchlist") {
      universeValue = form.universe_value.trim() || "default";
    } else if (form.universe_type === "screening") {
      universeValue = form.universe_value.trim();
      if (!universeValue) errors.push("请选择引用的选股配置");
    } else if (form.universe_type === "codes") {
      universeValue = form.universe_value.trim();
      if (!universeValue) errors.push("请填写标的代码（逗号分隔）");
    }

    if (!Number.isFinite(form.horizon) || form.horizon <= 0) errors.push("持有期必须是正整数");
    if (!Number.isFinite(form.min_bars) || form.min_bars <= 0) {
      errors.push("最少 K 线数必须是正整数");
    }
    if (form.start && form.end && form.start > form.end) errors.push("开始日期不能晚于结束日期");
    if (errors.length > 0) {
      setNotice({ kind: "error", text: errors.join("；") });
      return;
    }

    const input: SampleConfigInput = {
      name: form.name.trim(),
      description: form.description.trim(),
      universe_type: form.universe_type,
      universe_value: universeValue,
      start_date: form.start || null,
      end_date: form.end || null,
      lookback_days: form.lookback_days > 0 ? Math.trunc(form.lookback_days) : null,
      horizon: Math.trunc(form.horizon),
      min_bars: Math.trunc(form.min_bars),
    };
    setNotice(null);
    saveConfig.mutate({ id: editingId, input });
  };

  const describeUniverse = (config: SampleConfig): string => {
    const label = UNIVERSE_LABEL[config.universe_type];
    if (config.universe_type === "default" || !config.universe_value) return label;
    if (config.universe_type === "screening") {
      const match = screenings.data?.find((item) => String(item.id) === config.universe_value);
      return `${label}：${match?.name ?? `#${config.universe_value}`}`;
    }
    return `${label}：${config.universe_value}`;
  };

  const resolvedPreview = resolveResult
    ? resolveResult.data.codes.slice(0, 30).join(", ") +
      (resolveResult.data.codes.length > 30
        ? ` …（共 ${resolveResult.data.codes.length} 只）`
        : "")
    : "";

  return (
    <div className="space-y-4">
      {notice && <NoticeLine notice={notice} />}

      {/* 编辑表单 */}
      <div className="card space-y-3">
        <div className="text-sm font-medium text-slate-300">
          {editingId === null ? "新建样本配置" : `编辑样本配置 #${editingId}`}
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-4">
          <div className="sm:col-span-2">
            <label className="label">配置名称</label>
            <input
              className="input w-full"
              value={form.name}
              onChange={(event) => setSampleForm({ name: event.target.value })}
              placeholder="例如：近两年全市场样本"
            />
          </div>
          <div className="sm:col-span-2">
            <label className="label">说明（可选）</label>
            <input
              className="input w-full"
              value={form.description}
              onChange={(event) => setSampleForm({ description: event.target.value })}
            />
          </div>
          <div>
            <label className="label">标的来源</label>
            <select
              className="input w-full"
              value={form.universe_type}
              onChange={(event) => changeUniverseType(event.target.value as SampleUniverseType)}
            >
              <option value="watchlist">自选股清单</option>
              <option value="screening">引用选股配置</option>
              <option value="codes">指定代码</option>
              <option value="default">默认宇宙</option>
            </select>
          </div>
          <div>
            {form.universe_type === "watchlist" && (
              <>
                <label className="label">清单名称</label>
                <input
                  className="input w-full"
                  value={form.universe_value}
                  onChange={(event) => setSampleForm({ universe_value: event.target.value })}
                  placeholder="default"
                />
              </>
            )}
            {form.universe_type === "screening" && (
              <>
                <label className="label">引用的选股配置</label>
                <select
                  className="input w-full"
                  value={form.universe_value}
                  onChange={(event) => setSampleForm({ universe_value: event.target.value })}
                >
                  <option value="">请选择</option>
                  {(screenings.data ?? []).map((config) => (
                    <option key={config.id} value={String(config.id)}>
                      {config.name}
                    </option>
                  ))}
                </select>
                {screenings.isSuccess && screenings.data.length === 0 && (
                  <p className="mt-1 text-xs text-amber-300">还没有选股配置，请先在「选股配置」创建。</p>
                )}
              </>
            )}
            {form.universe_type === "codes" && (
              <>
                <label className="label">代码（逗号分隔）</label>
                <input
                  className="input w-full"
                  value={form.universe_value}
                  onChange={(event) => setSampleForm({ universe_value: event.target.value })}
                  placeholder="600519.SH,000001.SZ"
                />
              </>
            )}
            {form.universe_type === "default" && (
              <>
                <label className="label">说明</label>
                <p className="text-xs text-slate-500">使用系统默认标的宇宙（全部可用标的）。</p>
              </>
            )}
          </div>
          <div>
            <label className="label">开始日期（可选）</label>
            <input
              type="date"
              className="input w-full"
              value={form.start}
              onChange={(event) => setSampleForm({ start: event.target.value })}
            />
          </div>
          <div>
            <label className="label">结束日期（可选）</label>
            <input
              type="date"
              className="input w-full"
              value={form.end}
              onChange={(event) => setSampleForm({ end: event.target.value })}
            />
          </div>
          <div>
            <label className="label">回看天数（0=不限）</label>
            <input
              type="number"
              min={0}
              className="input w-full"
              value={form.lookback_days}
              onChange={(event) => setSampleForm({ lookback_days: Number(event.target.value) })}
            />
          </div>
          <div>
            <label className="label">持有期 horizon（交易日）</label>
            <input
              type="number"
              min={1}
              className="input w-full"
              value={form.horizon}
              onChange={(event) => setSampleForm({ horizon: Number(event.target.value) })}
            />
          </div>
          <div>
            <label className="label">最少 K 线数 min_bars</label>
            <input
              type="number"
              min={1}
              className="input w-full"
              value={form.min_bars}
              onChange={(event) => setSampleForm({ min_bars: Number(event.target.value) })}
            />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            className="btn btn-primary"
            onClick={submit}
            disabled={saveConfig.isPending || readOnly}
          >
            {saveConfig.isPending ? "保存中…" : editingId === null ? "创建配置" : "保存修改"}
          </button>
          <button className="btn" onClick={startCreate} disabled={readOnly}>
            {editingId === null ? "重置表单" : "取消编辑"}
          </button>
          <span className="text-xs text-slate-500">
            解析后的样本在训练时按 horizon 生成标签，min_bars 过滤历史太短的标的。
          </span>
        </div>
      </div>

      {/* 配置列表 */}
      <div className="card">
        <div className="mb-2 text-sm font-medium text-slate-300">样本配置列表</div>
        {configs.isPending && <p className="text-xs text-slate-500">加载中…</p>}
        {configs.isError && <p className="text-xs text-amber-300">{errorText(configs.error)}</p>}
        {configs.data && configs.data.length === 0 && (
          <p className="text-xs text-slate-500">尚未创建样本配置。</p>
        )}
        {configs.data && configs.data.length > 0 && (
          <div className="table-wrap">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-800">
                  <th className="th">名称</th>
                  <th className="th">标的来源</th>
                  <th className="th hidden sm:table-cell">时间区间</th>
                  <th className="th text-right">持有期</th>
                  <th className="th text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {configs.data.map((config) => {
                  const resolving =
                    resolveConfig.isPending && resolveConfig.variables?.id === config.id;
                  return (
                    <tr key={config.id} className="border-t border-slate-800/60">
                      <td className="td">
                        {config.name}
                        {config.description && (
                          <span className="ml-1 hidden text-xs text-slate-500 sm:inline">
                            {config.description}
                          </span>
                        )}
                      </td>
                      <td className="td text-slate-400">{describeUniverse(config)}</td>
                      <td className="td hidden text-xs text-slate-500 sm:table-cell">
                        {config.start_date ?? "不限"} ~ {config.end_date ?? "最新"}
                      </td>
                      <td className="td text-right">{config.horizon}</td>
                      <td className="td text-right">
                        <button
                          className="mr-2 text-xs text-sky-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
                          disabled={resolving}
                          onClick={() => resolveConfig.mutate({ id: config.id, name: config.name })}
                        >
                          {resolving ? "解析中…" : "解析"}
                        </button>
                        <button
                          className="mr-2 text-xs text-sky-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
                          disabled={readOnly}
                          onClick={() => startEdit(config)}
                        >
                          编辑
                        </button>
                        <button
                          className="text-xs text-red-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
                          disabled={readOnly || removeConfig.isPending}
                          onClick={() => {
                            if (!window.confirm(`确认删除样本配置「${config.name}」？`)) return;
                            removeConfig.mutate(config.id);
                          }}
                        >
                          删除
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 解析结果 */}
      {resolveResult && (
        <div className="card space-y-2">
          <div className="text-sm font-medium text-slate-300">
            解析结果 <span className="text-xs text-slate-500">「{resolveResult.name}」</span>
          </div>
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm md:grid-cols-4">
            <Meta label="标的数量" value={String(resolveResult.data.count)} />
            <Meta label="开始日期" value={resolveResult.data.start ?? "不限"} />
            <Meta label="结束日期" value={resolveResult.data.end ?? "最新"} />
            <Meta label="持有期" value={String(resolveResult.data.horizon)} />
          </div>
          <p className="break-all text-xs text-slate-400">
            代码预览：{resolvedPreview || "（空）"}
          </p>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 算法配置
// ---------------------------------------------------------------------------

function AlgorithmPanel() {
  const queryClient = useQueryClient();
  const workspace = useCurrentWorkspace();
  const readOnly = !canWriteWorkspace(workspace);
  const { algorithm: form } = useViewState((state) => state.strategy);
  const setAlgorithmForm = useViewState((state) => state.setAlgorithmForm);

  const [editingId, setEditingId] = useState<number | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [trainResult, setTrainResult] = useState<{
    name: string;
    data: AlgorithmTrainResult;
  } | null>(null);

  const configs = useQuery({
    queryKey: ["strategy", "algorithms", workspace?.id],
    queryFn: api.algorithmConfigs,
  });
  // 算法配置的 sample_id 下拉需要已加载的样本配置。
  const samples = useQuery({
    queryKey: ["strategy", "samples", workspace?.id],
    queryFn: api.sampleConfigs,
  });

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: ["strategy", "algorithms"] });

  const saveConfig = useMutation({
    mutationFn: (vars: { id: number | null; input: AlgorithmConfigInput }) =>
      vars.id === null
        ? api.createAlgorithmConfig(vars.input)
        : api.updateAlgorithmConfig(vars.id, vars.input),
    onSuccess: (_data, vars) => {
      setNotice({ kind: "ok", text: vars.id === null ? "算法配置已创建" : "算法配置已保存" });
      invalidate();
    },
    onError: (error) => setNotice({ kind: "error", text: errorText(error) }),
  });

  const removeConfig = useMutation({
    mutationFn: (id: number) => api.deleteAlgorithmConfig(id),
    onSuccess: (_data, id) => {
      setNotice({ kind: "ok", text: "算法配置已删除" });
      setEditingId((current) => (current === id ? null : current));
      invalidate();
    },
    onError: (error) => setNotice({ kind: "error", text: errorText(error) }),
  });

  const trainConfig = useMutation({
    mutationFn: (vars: { id: number; name: string }) => api.trainAlgorithmConfig(vars.id),
    onSuccess: (data, vars) => {
      setTrainResult({ name: vars.name, data });
      setNotice({
        kind: "ok",
        text: `训练完成：模型 ${data.model_id}（训练 ${data.n_train} 条 / 测试 ${data.n_test} 条）`,
      });
      // 训练会新增模型，模型列表页/预测页需要重新取数。
      queryClient.invalidateQueries({ queryKey: ["models"] });
    },
    onError: (error) => setNotice({ kind: "error", text: errorText(error) }),
  });

  const resetFields = () => {
    setAlgorithmForm({
      name: "",
      description: "",
      algo: "hist_gbdt",
      sample_id: "",
      horizon: 5,
      test_fraction: "0.2",
      params: { ...ALGO_PARAMS.hist_gbdt },
    });
    setEditingId(null);
  };

  const startCreate = () => {
    resetFields();
    setNotice(null);
  };

  const startEdit = (config: AlgorithmConfig) => {
    fillAlgorithmForm(config, setAlgorithmForm);
    setEditingId(config.id);
    setNotice({ kind: "ok", text: `已把「${config.name}」载入表单` });
  };

  const changeAlgo = (next: string) => {
    // 超参名与算法强相关（sklearn 参数），切算法时替换为该算法的默认参数。
    setAlgorithmForm({ algo: next, params: { ...(ALGO_PARAMS[next] ?? {}) } });
  };

  const addParam = () => {
    let index = Object.keys(form.params).length + 1;
    let key = `param_${index}`;
    while (Object.prototype.hasOwnProperty.call(form.params, key)) {
      index += 1;
      key = `param_${index}`;
    }
    setAlgorithmForm({ params: { ...form.params, [key]: "" } });
  };

  const renameParam = (oldKey: string, nextKey: string) => {
    if (nextKey !== oldKey && Object.prototype.hasOwnProperty.call(form.params, nextKey)) return;
    const params: Record<string, string> = {};
    Object.entries(form.params).forEach(([key, value]) => {
      params[key === oldKey ? nextKey : key] = value;
    });
    setAlgorithmForm({ params });
  };

  const setParamValue = (key: string, value: string) => {
    setAlgorithmForm({ params: { ...form.params, [key]: value } });
  };

  const removeParam = (key: string) => {
    const params = { ...form.params };
    delete params[key];
    setAlgorithmForm({ params });
  };

  const submit = () => {
    const errors: string[] = [];
    if (!form.name.trim()) errors.push("请填写配置名称");
    if (!Number.isFinite(form.horizon) || form.horizon <= 0) errors.push("持有期必须是正整数");
    if (form.test_fraction.trim() !== "") {
      const fraction = Number(form.test_fraction);
      if (!Number.isFinite(fraction) || fraction <= 0 || fraction >= 1) {
        errors.push("测试集比例必须在 0 与 1 之间");
      }
    }
    if (form.sample_id !== "" && !Number.isFinite(Number(form.sample_id))) {
      errors.push("样本配置 ID 非法");
    }
    Object.entries(form.params).forEach(([rawKey, rawValue]) => {
      const key = rawKey.trim();
      const value = rawValue.trim();
      if (!key) {
        errors.push("超参名不能为空");
        return;
      }
      if (value !== "" && !Number.isFinite(Number(value))) {
        errors.push(`超参 ${key} 必须是数字`);
      }
    });
    if (errors.length > 0) {
      setNotice({ kind: "error", text: errors.join("；") });
      return;
    }

    const input: AlgorithmConfigInput = {
      name: form.name.trim(),
      description: form.description.trim(),
      algo: form.algo,
      params: coerceParams(form.params),
      sample_id: form.sample_id === "" ? null : Number(form.sample_id),
      horizon: Math.trunc(form.horizon),
      test_fraction: form.test_fraction.trim() === "" ? null : Number(form.test_fraction),
    };
    setNotice(null);
    saveConfig.mutate({ id: editingId, input });
  };

  const sampleName = (id: number | null): string => {
    if (id === null) return "不绑定";
    return samples.data?.find((item) => item.id === id)?.name ?? `#${id}`;
  };

  return (
    <div className="space-y-4">
      {notice && <NoticeLine notice={notice} />}

      {/* 编辑表单 */}
      <div className="card space-y-3">
        <div className="text-sm font-medium text-slate-300">
          {editingId === null ? "新建算法配置" : `编辑算法配置 #${editingId}`}
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-4">
          <div className="sm:col-span-2">
            <label className="label">配置名称</label>
            <input
              className="input w-full"
              value={form.name}
              onChange={(event) => setAlgorithmForm({ name: event.target.value })}
              placeholder="例如：GBDT 5 日方向"
            />
          </div>
          <div className="sm:col-span-2">
            <label className="label">说明（可选）</label>
            <input
              className="input w-full"
              value={form.description}
              onChange={(event) => setAlgorithmForm({ description: event.target.value })}
            />
          </div>
          <div>
            <label className="label">算法 algo</label>
            <select
              className="input w-full"
              value={form.algo}
              onChange={(event) => changeAlgo(event.target.value)}
            >
              {ALGO_OPTIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="label">训练样本（可为空）</label>
            <select
              className="input w-full"
              value={form.sample_id}
              onChange={(event) => setAlgorithmForm({ sample_id: event.target.value })}
            >
              <option value="">不绑定样本</option>
              {(samples.data ?? []).map((sample) => (
                <option key={sample.id} value={String(sample.id)}>
                  {sample.name}
                </option>
              ))}
            </select>
            {samples.isSuccess && samples.data.length === 0 && (
              <p className="mt-1 text-xs text-slate-500">可先在「样本配置」创建样本。</p>
            )}
          </div>
          <div>
            <label className="label">持有期 horizon（交易日）</label>
            <input
              type="number"
              min={1}
              className="input w-full"
              value={form.horizon}
              onChange={(event) => setAlgorithmForm({ horizon: Number(event.target.value) })}
            />
          </div>
          <div>
            <label className="label">测试集比例 test_fraction</label>
            <input
              type="number"
              step="0.05"
              min={0}
              max={1}
              className="input w-full"
              value={form.test_fraction}
              onChange={(event) => setAlgorithmForm({ test_fraction: event.target.value })}
              placeholder="留空=默认"
            />
          </div>

          <div className="sm:col-span-2 md:col-span-4">
            <div className="mb-1 flex items-center justify-between">
              <label className="label mb-0">超参数（名称 / 值）</label>
              <button className="btn" onClick={addParam} disabled={readOnly}>
                添加参数
              </button>
            </div>
            <div className="space-y-2">
              {Object.entries(form.params).map(([key, value], index) => (
                // 用下标做 key：改参数名时行身份不变，输入框不会因 key 变化而失焦。
                <div key={index} className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_1fr_auto]">
                  <input
                    className="input w-full font-mono"
                    value={key}
                    onChange={(event) => renameParam(key, event.target.value)}
                    placeholder="参数名"
                  />
                  <input
                    className="input w-full font-mono"
                    value={value}
                    onChange={(event) => setParamValue(key, event.target.value)}
                    placeholder="值（数字 / true / false / 字符串）"
                  />
                  <button className="btn" onClick={() => removeParam(key)} disabled={readOnly}>
                    删除
                  </button>
                </div>
              ))}
              {Object.keys(form.params).length === 0 && (
                <p className="text-xs text-slate-500">暂无超参数，将使用训练函数的默认值。</p>
              )}
            </div>
            <p className="mt-1 text-xs text-slate-500">
              数字按数字提交，true/false 按布尔提交，其余按字符串；值为空则不传该项。
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button
            className="btn btn-primary"
            onClick={submit}
            disabled={saveConfig.isPending || readOnly}
          >
            {saveConfig.isPending ? "保存中…" : editingId === null ? "创建配置" : "保存修改"}
          </button>
          <button className="btn" onClick={startCreate} disabled={readOnly}>
            {editingId === null ? "重置表单" : "取消编辑"}
          </button>
          <span className="text-xs text-slate-500">
            训练会生成新模型，可在「预测」页用于推断；不绑定样本时使用全市场默认样本。
          </span>
        </div>
      </div>

      {/* 配置列表 */}
      <div className="card">
        <div className="mb-2 text-sm font-medium text-slate-300">算法配置列表</div>
        {configs.isPending && <p className="text-xs text-slate-500">加载中…</p>}
        {configs.isError && <p className="text-xs text-amber-300">{errorText(configs.error)}</p>}
        {configs.data && configs.data.length === 0 && (
          <p className="text-xs text-slate-500">尚未创建算法配置。</p>
        )}
        {configs.data && configs.data.length > 0 && (
          <div className="table-wrap">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-800">
                  <th className="th">名称</th>
                  <th className="th">算法</th>
                  <th className="th hidden sm:table-cell">训练样本</th>
                  <th className="th text-right">持有期</th>
                  <th className="th hidden text-right md:table-cell">测试集比例</th>
                  <th className="th text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {configs.data.map((config) => {
                  const training =
                    trainConfig.isPending && trainConfig.variables?.id === config.id;
                  return (
                    <tr key={config.id} className="border-t border-slate-800/60">
                      <td className="td">
                        {config.name}
                        {config.description && (
                          <span className="ml-1 hidden text-xs text-slate-500 sm:inline">
                            {config.description}
                          </span>
                        )}
                      </td>
                      <td className="td">
                        <span className="badge bg-slate-800 text-slate-300">{config.algo}</span>
                      </td>
                      <td className="td hidden text-slate-400 sm:table-cell">
                        {sampleName(config.sample_id)}
                      </td>
                      <td className="td text-right">{config.horizon}</td>
                      <td className="td hidden text-right text-slate-400 md:table-cell">
                        {config.test_fraction ?? "--"}
                      </td>
                      <td className="td text-right">
                        <button
                          className="mr-2 text-xs text-sky-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
                          disabled={readOnly || training}
                          onClick={() => trainConfig.mutate({ id: config.id, name: config.name })}
                        >
                          {training ? "训练中…" : "训练"}
                        </button>
                        <button
                          className="mr-2 text-xs text-sky-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
                          disabled={readOnly}
                          onClick={() => startEdit(config)}
                        >
                          编辑
                        </button>
                        <button
                          className="text-xs text-red-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
                          disabled={readOnly || removeConfig.isPending}
                          onClick={() => {
                            if (!window.confirm(`确认删除算法配置「${config.name}」？`)) return;
                            removeConfig.mutate(config.id);
                          }}
                        >
                          删除
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* 训练结果 */}
      {trainResult && (
        <div className="card space-y-2">
          <div className="text-sm font-medium text-slate-300">
            训练结果 <span className="text-xs text-slate-500">「{trainResult.name}」</span>
          </div>
          <div className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm md:grid-cols-4">
            <Meta label="模型 ID" value={trainResult.data.model_id} />
            <Meta label="算法" value={trainResult.data.algo} />
            <Meta label="持有期" value={String(trainResult.data.horizon)} />
            <Meta label="训练样本数" value={String(trainResult.data.n_train)} />
            <Meta label="测试样本数" value={String(trainResult.data.n_test)} />
            <Meta label="AUC" value={metricText(trainResult.data.metrics, "auc")} />
            <Meta label="IC (Spearman)" value={metricText(trainResult.data.metrics, "ic_spearman")} />
          </div>
          <details className="text-xs text-slate-400">
            <summary className="cursor-pointer text-slate-500">全部指标</summary>
            <pre className="mt-2 overflow-x-auto rounded bg-slate-950/70 p-2 text-[11px] text-slate-400">
              {JSON.stringify(trainResult.data.metrics, null, 2)}
            </pre>
          </details>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 共享小工具
// ---------------------------------------------------------------------------

type Notice = { kind: "ok" | "error"; text: string };

function NoticeLine({ notice }: { notice: Notice }) {
  return (
    <p
      className={notice.kind === "error" ? "text-sm text-amber-300" : "text-sm text-sky-300"}
      role="status"
    >
      {notice.text}
    </p>
  );
}

/** 训练结果里的键值对展示（模型 ID 等不是数字，不能复用回测的 Stat）。 */
function Meta({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3 border-b border-slate-800/50 py-0.5">
      <span className="text-slate-400">{label}</span>
      <span className="truncate text-slate-200" title={value}>
        {value}
      </span>
    </div>
  );
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** 数字草稿是否为「空或合法数字」——用于提交前校验，避免 Number("abc") 静默变 NaN。 */
function isBlankOrNumber(raw: string): boolean {
  const text = raw.trim();
  return text === "" || Number.isFinite(Number(text));
}

/** 空字符串 -> null（后端的可选数值字段语义）。 */
function optionalNumber(raw: string): number | null {
  const text = raw.trim();
  if (text === "" || !Number.isFinite(Number(text))) return null;
  return Number(text);
}

/** 板块允许逗号（中英文）或空格分隔。 */
function splitBoards(raw: string): string[] {
  return raw
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

/** 条件行：field op value，例如 mom_20>=0.5。 */
const CONDITION_RE = /^([^\s<>=]+)\s*(>=|<=|==|>|<)\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)$/;

function parseConditions(raw: string): { conditions: ScreeningCondition[]; errors: string[] } {
  const conditions: ScreeningCondition[] = [];
  const errors: string[] = [];
  raw.split(/\r?\n/).forEach((line, index) => {
    const text = line.trim();
    if (text === "" || text.startsWith("#")) return;
    const match = CONDITION_RE.exec(text);
    if (!match) {
      errors.push(`第 ${index + 1} 行无法解析：${text}（示例 mom_20>=0.5）`);
      return;
    }
    conditions.push({ field: match[1], op: match[2] as ScreeningOp, value: Number(match[3]) });
  });
  return { conditions, errors };
}

/** 列表里的参数摘要：只展示关键项，避免一行 JSON 撑爆表格。 */
function screeningSummary(params: ScreeningParams): string {
  const parts: string[] = [];
  if (params.min_score !== null) parts.push(`评分≥${params.min_score}`);
  if (params.max_vol !== null) parts.push(`波动率≤${params.max_vol}`);
  if (params.min_amount_log !== null) parts.push(`成交额对数≥${params.min_amount_log}`);
  if (params.boards.length > 0) parts.push(`板块 ${params.boards.join("/")}`);
  if (params.conditions.length > 0) parts.push(`条件 ${params.conditions.length} 条`);
  if (params.exclude_st) parts.push("排除 ST");
  parts.push(`按 ${params.order_by} ${params.direction === "desc" ? "降序" : "升序"}`);
  parts.push(`上限 ${params.limit}`);
  return parts.join(" · ");
}

/** 摘要的悬停详情：完整条件列表。 */
function conditionTitle(params: ScreeningParams): string {
  if (params.conditions.length === 0) return "无自定义条件";
  return params.conditions.map((item) => `${item.field}${item.op}${item.value}`).join("\n");
}

/** 编辑：把配置行填回表单草稿（数值/数组转成输入框友好的字符串）。 */
function fillScreeningForm(
  config: ScreeningConfig,
  setForm: (patch: Partial<ScreeningConfigForm>) => void,
): void {
  const params = config.params;
  setForm({
    name: config.name,
    description: config.description,
    min_score: params.min_score === null ? "" : String(params.min_score),
    max_vol: params.max_vol === null ? "" : String(params.max_vol),
    min_amount_log: params.min_amount_log === null ? "" : String(params.min_amount_log),
    exclude_st: params.exclude_st,
    boards: params.boards.join(", "),
    conditions: params.conditions.map((item) => `${item.field}${item.op}${item.value}`).join("\n"),
    order_by: params.order_by,
    direction: params.direction,
    limit: params.limit,
  });
}

function fillSampleForm(
  config: SampleConfig,
  setForm: (patch: Partial<SampleConfigForm>) => void,
): void {
  setForm({
    name: config.name,
    description: config.description,
    universe_type: config.universe_type,
    universe_value: config.universe_value,
    start: config.start_date ?? "",
    end: config.end_date ?? "",
    lookback_days: config.lookback_days ?? 0,
    horizon: config.horizon,
    min_bars: config.min_bars,
  });
}

function fillAlgorithmForm(
  config: AlgorithmConfig,
  setForm: (patch: Partial<AlgorithmConfigForm>) => void,
): void {
  const params: Record<string, string> = {};
  Object.entries(config.params).forEach(([key, value]) => {
    params[key] = String(value);
  });
  setForm({
    name: config.name,
    description: config.description,
    algo: config.algo,
    sample_id: config.sample_id === null ? "" : String(config.sample_id),
    horizon: config.horizon,
    test_fraction: config.test_fraction === null ? "" : String(config.test_fraction),
    params,
  });
}

/** 超参字符串草稿 -> 请求体：只接受有限数字；空值不传。
 *
 * 后端按算法白名单校验超参的类型与范围（布尔/字符串会 422），所以这里提前拦下，
 * 而不是让用户保存后才看到 422。
 */
function coerceParams(params: Record<string, string>): Record<string, number> {
  const out: Record<string, number> = {};
  Object.entries(params).forEach(([rawKey, rawValue]) => {
    const key = rawKey.trim();
    const value = rawValue.trim();
    if (!key || value === "") return;
    out[key] = Number(value);
  });
  return out;
}

/** 训练指标可能是数字/字符串/缺失，统一转成可展示文本。 */
function metricText(metrics: Record<string, unknown>, key: string): string {
  const value = metrics[key];
  if (typeof value === "number") return Number.isFinite(value) ? value.toFixed(4) : "--";
  if (typeof value === "string") return value;
  if (value === null || value === undefined) return "--";
  return JSON.stringify(value);
}
