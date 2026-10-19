import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import SymbolInput from "../components/SymbolInput";
import { canWriteWorkspace, useCurrentWorkspace } from "../workspace";
import { useViewState } from "../view";
import type { RuleTypeInfo } from "../types";

export default function Alerts() {
  const queryClient = useQueryClient();
  const workspace = useCurrentWorkspace();
  const readOnly = !canWriteWorkspace(workspace);
  const { statusFilter, form, params } = useViewState((state) => state.alerts);
  const setAlerts = useViewState((state) => state.setAlerts);
  const setAlertForm = useViewState((state) => state.setAlertForm);
  const [message, setMessage] = useState<string | null>(null);

  const ruleTypes = useQuery({ queryKey: ["ruleTypes"], queryFn: api.ruleTypes });
  const rules = useQuery({ queryKey: ["alertRules"], queryFn: api.alertRules });
  const triggers = useQuery({
    queryKey: ["triggers", statusFilter],
    queryFn: () => api.triggers(statusFilter || undefined),
    refetchInterval: 30_000,
  });

  const currentType: RuleTypeInfo | undefined = useMemo(
    () => ruleTypes.data?.find((item) => item.rule_type === form.rule_type),
    [ruleTypes.data, form.rule_type],
  );

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["alertRules"] });
    queryClient.invalidateQueries({ queryKey: ["triggers"] });
  };

  const createRule = useMutation({
    mutationFn: () =>
      api.createRule({
        ...form,
        min_amount: Number(form.min_amount),
        params: coerceParams(params),
      }),
    onSuccess: () => {
      setMessage("规则已创建");
      invalidate();
    },
    onError: (error) => setMessage((error as Error).message),
  });

  const toggleRule = useMutation({
    mutationFn: ({ id, enabled }: { id: number; enabled: boolean }) => {
      const rule = rules.data?.find((item) => item.id === id);
      if (!rule) throw new Error(`规则 ${id} 不存在`);
      // Send the full definition back: the update endpoint replaces the rule.
      return api.updateRule(id, {
        name: rule.name,
        rule_type: rule.rule_type,
        params: rule.params,
        scope_type: rule.scope_type,
        scope_value: rule.scope_value,
        cooldown_seconds: rule.cooldown_seconds,
        once_per_day: rule.once_per_day,
        exclude_st: rule.exclude_st,
        min_amount: rule.min_amount,
        enabled,
      });
    },
    onSuccess: () => invalidate(),
  });

  const deleteRule = useMutation({
    mutationFn: (id: number) => api.deleteRule(id),
    onSuccess: () => invalidate(),
  });

  const scan = useMutation({
    mutationFn: api.scanAlerts,
    onSuccess: (data) => {
      setMessage(`扫描完成，新增 ${data.triggered} 条预警`);
      invalidate();
    },
  });

  const preview = useMutation({
    mutationFn: () =>
      api.previewRule({
        ...form,
        min_amount: Number(form.min_amount),
        params: coerceParams(params),
      }),
    onSuccess: (data) =>
      setMessage(`预览：范围 ${data.scope_size} 只，命中 ${data.matched} 只`),
    onError: (error) => setMessage((error as Error).message),
  });

  const updateTrigger = useMutation({
    mutationFn: ({ id, status }: { id: number; status: string }) =>
      api.updateTrigger(id, status),
    onSuccess: () => invalidate(),
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        {readOnly && (
          <span className="text-xs text-amber-300">
            当前工作区为只读角色（viewer），规则与触发记录不可修改，仅可预览。
          </span>
        )}
        <button
          className="btn btn-primary"
          onClick={() => scan.mutate()}
          disabled={scan.isPending || readOnly}
        >
          {scan.isPending ? "扫描中…" : "立即扫描"}
        </button>
        <button className="btn" onClick={() => preview.mutate()} disabled={preview.isPending}>
          预览规则命中
        </button>
        {message && <span className="text-sm text-sky-300">{message}</span>}
      </div>

      {/* Rule editor */}
      <div className="card space-y-3">
        <div className="text-sm font-medium text-slate-300">新建预警规则</div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-4">
          <div className="sm:col-span-2">
            <label className="label">规则名称</label>
            <input
              className="input w-full"
              value={form.name}
              onChange={(e) => setAlertForm({ name: e.target.value })}
            />
          </div>
          <div className="sm:col-span-2">
            <label className="label">规则类型</label>
            <select
              className="input w-full"
              value={form.rule_type}
              onChange={(e) => {
                const next = e.target.value;
                const spec = ruleTypes.data?.find((item) => item.rule_type === next);
                const defaults: Record<string, string> = {};
                Object.entries(spec?.params ?? {}).forEach(([key, param]) => {
                  if (param.default !== undefined) defaults[key] = String(param.default);
                });
                setAlerts({ params: defaults });
                setAlertForm({ rule_type: next });
              }}
            >
              {(ruleTypes.data ?? []).map((item) => (
                <option key={item.rule_type} value={item.rule_type}>
                  {item.rule_type} — {item.description}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="label">范围类型</label>
            <select
              className="input w-full"
              value={form.scope_type}
              onChange={(e) => setAlertForm({ scope_type: e.target.value })}
            >
              <option value="watchlist">自选股清单</option>
              <option value="codes">指定代码</option>
              <option value="index">指数成分</option>
              <option value="all">全市场</option>
            </select>
          </div>
          <div>
            <label className="label">
              {form.scope_type === "watchlist"
                ? "清单名称"
                : form.scope_type === "index"
                  ? "指数代码"
                  : "代码（逗号分隔）"}
            </label>
            {form.scope_type === "watchlist" ? (
              <input
                className="input w-full"
                value={form.scope_value}
                onChange={(e) => setAlertForm({ scope_value: e.target.value })}
              />
            ) : (
              <SymbolInput
                multiple={form.scope_type !== "index"}
                includeIndices={form.scope_type === "index"}
                value={form.scope_value}
                onChange={(next) => setAlertForm({ scope_value: next })}
                placeholder={
                  form.scope_type === "index"
                    ? "如 000300.SH"
                    : "输入代码或名称联想，如 600519 / 茅台"
                }
                aria-label={form.scope_type === "index" ? "指数代码" : "范围代码"}
              />
            )}
          </div>
          <div>
            <label className="label">最低成交额（元）</label>
            <input
              type="number"
              className="input w-full"
              value={form.min_amount}
              onChange={(e) => setAlertForm({ min_amount: Number(e.target.value) })}
            />
          </div>
          <div className="flex items-end gap-4">
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={form.once_per_day}
                onChange={(e) => setAlertForm({ once_per_day: e.target.checked })}
              />
              每日仅一次
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={form.exclude_st}
                onChange={(e) => setAlertForm({ exclude_st: e.target.checked })}
              />
              排除 ST
            </label>
          </div>
        </div>

        {/* Dynamic parameters driven by the backend schema */}
        {currentType && (
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 md:grid-cols-4">
            {Object.entries(currentType.params).map(([key, spec]) => (
              <div key={key}>
                <label className="label">
                  {key}
                  {spec.required ? " *" : ""}
                </label>
                {spec.choices ? (
                  <select
                    className="input w-full"
                    value={params[key] ?? String(spec.default ?? "")}
                    onChange={(e) => setAlerts({ params: { ...params, [key]: e.target.value } })}
                  >
                    {spec.choices.map((choice) => (
                      <option key={String(choice)} value={String(choice)}>
                        {String(choice)}
                      </option>
                    ))}
                  </select>
                ) : (
                  <input
                    className="input w-full"
                    value={params[key] ?? String(spec.default ?? "")}
                    onChange={(e) => setAlerts({ params: { ...params, [key]: e.target.value } })}
                  />
                )}
              </div>
            ))}
          </div>
        )}

        <button
          className="btn btn-primary"
          onClick={() => createRule.mutate()}
          disabled={createRule.isPending || readOnly}
        >
          {createRule.isPending ? "创建中…" : "创建规则"}
        </button>
        <p className="text-xs text-slate-500">
          预警按 (规则, 标的, 当日) 去重，重复扫描不会重复提醒。盘中每 60 秒轮询一次快照。
        </p>
      </div>

      {/* Rules */}
      <div className="card">
        <div className="mb-2 text-sm font-medium text-slate-300">规则列表</div>
        {rules.data?.length ? (
          <div className="table-wrap">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-800">
                  <th className="th">名称</th>
                  <th className="th hidden md:table-cell">类型</th>
                  <th className="th hidden md:table-cell">参数</th>
                  <th className="th">范围</th>
                  <th className="th">状态</th>
                  <th className="th text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {rules.data.map((rule) => (
                  <tr key={rule.id} className="border-t border-slate-800/60">
                    <td className="td">{rule.name}</td>
                    <td className="td hidden text-slate-400 md:table-cell">{rule.rule_type}</td>
                    <td className="td hidden max-w-[220px] whitespace-normal text-xs text-slate-500 md:table-cell">
                      {JSON.stringify(rule.params)}
                    </td>
                    <td
                      className="td max-w-[8rem] truncate text-slate-400"
                      title={`${rule.scope_type}:${rule.scope_value || "-"}`}
                    >
                      {rule.scope_type}:{rule.scope_value || "-"}
                    </td>
                    <td className="td">
                      <span
                        className={`badge ${
                          rule.enabled
                            ? "bg-emerald-900/60 text-emerald-300"
                            : "bg-slate-800 text-slate-400"
                        }`}
                      >
                        {rule.enabled ? "启用" : "停用"}
                      </span>
                    </td>
                    <td className="td text-right">
                      <button
                        className="mr-2 text-xs text-sky-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
                        disabled={readOnly}
                        onClick={() => toggleRule.mutate({ id: rule.id, enabled: !rule.enabled })}
                      >
                        {rule.enabled ? "停用" : "启用"}
                      </button>
                      <button
                        className="text-xs text-red-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
                        disabled={readOnly}
                        onClick={() => deleteRule.mutate(rule.id)}
                      >
                        删除
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-xs text-slate-500">尚未创建规则。</p>
        )}
      </div>

      {/* Triggers */}
      <div className="card">
        <div className="mb-2 flex items-center gap-3">
          <span className="text-sm font-medium text-slate-300">触发记录</span>
          <select
            className="input"
            value={statusFilter}
            onChange={(e) => setAlerts({ statusFilter: e.target.value })}
          >
            <option value="">全部</option>
            <option value="new">未读</option>
            <option value="read">已读</option>
            <option value="muted">静音</option>
          </select>
        </div>
        {triggers.data?.length ? (
          <div className="table-wrap">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-800">
                  <th className="th hidden md:table-cell">日期</th>
                  <th className="th">代码</th>
                  <th className="th hidden md:table-cell">类型</th>
                  <th className="th">消息</th>
                  <th className="th">状态</th>
                  <th className="th text-right">操作</th>
                </tr>
              </thead>
              <tbody>
                {triggers.data.map((trigger) => (
                  <tr key={trigger.id} className="border-t border-slate-800/60">
                    <td className="td hidden text-slate-400 md:table-cell">{trigger.trade_date}</td>
                    <td className="td">
                      {trigger.code}
                      <span className="ml-1 hidden text-slate-500 sm:inline">{trigger.name}</span>
                    </td>
                    <td className="td hidden text-slate-400 md:table-cell">{trigger.rule_type}</td>
                    <td className="td min-w-[9rem] whitespace-normal">{trigger.message}</td>
                    <td className="td">
                      <span
                        className={`badge ${
                          trigger.status === "new"
                            ? "bg-sky-900/60 text-sky-300"
                            : "bg-slate-800 text-slate-400"
                        }`}
                      >
                        {trigger.status}
                      </span>
                    </td>
                    <td className="td text-right">
                      <button
                        className="mr-2 text-xs text-sky-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
                        disabled={readOnly}
                        onClick={() => updateTrigger.mutate({ id: trigger.id, status: "read" })}
                      >
                        已读
                      </button>
                      <button
                        className="text-xs text-slate-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
                        disabled={readOnly}
                        onClick={() => updateTrigger.mutate({ id: trigger.id, status: "muted" })}
                      >
                        静音
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-xs text-slate-500">暂无触发记录。</p>
        )}
      </div>
    </div>
  );
}

/** Rule params arrive from form inputs as strings; convert to numbers as needed. */
function coerceParams(params: Record<string, string>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  Object.entries(params).forEach(([key, value]) => {
    if (value === "") return;
    const numeric = Number(value);
    out[key] = Number.isNaN(numeric) ? value : numeric;
  });
  return out;
}
