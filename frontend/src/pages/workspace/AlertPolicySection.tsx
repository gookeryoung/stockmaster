/** 工作区级预警策略：成员只读「生效值」，owner 可覆盖。
 *
 * 四个字段为 null 表示「跟随规则」；策略只收紧、不放宽，所以两个布尔项只有
 * 「跟随规则 / 强制开启」两种选择。空数字输入同样表示跟随规则。
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api";
import ConfirmDialog from "../../components/ConfirmDialog";

interface Props {
  workspaceId: number;
  canManage: boolean;
  adminViewing?: boolean;
  onFlash: (text: string) => void;
}

export default function AlertPolicySection({ workspaceId, canManage, adminViewing = false, onFlash }: Props) {
  const queryClient = useQueryClient();

  const policy = useQuery({
    queryKey: ["alert-policy", workspaceId],
    queryFn: () => api.alertPolicy(workspaceId),
    // 表单会用返回数据回填；避免窗口重新聚焦时的静默重取覆盖正在编辑的内容。
    refetchOnWindowFocus: false,
    retry: false,
  });

  const [excludeSt, setExcludeSt] = useState<"follow" | "force">("follow");
  const [oncePerDay, setOncePerDay] = useState<"follow" | "force">("follow");
  const [minAmount, setMinAmount] = useState("");
  const [cooldownSeconds, setCooldownSeconds] = useState("");
  const [resetOpen, setResetOpen] = useState(false);

  useEffect(() => {
    const data = policy.data;
    if (!data) return;
    setExcludeSt(data.exclude_st ? "force" : "follow");
    setOncePerDay(data.once_per_day ? "force" : "follow");
    setMinAmount(data.min_amount === null ? "" : String(data.min_amount));
    setCooldownSeconds(data.cooldown_seconds === null ? "" : String(data.cooldown_seconds));
  }, [policy.data]);

  const parseOptionalNumber = (value: string): number | null => {
    const trimmed = value.trim();
    if (!trimmed) return null;
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) ? parsed : null;
  };

  const save = useMutation({
    mutationFn: () =>
      api.updateAlertPolicy(workspaceId, {
        exclude_st: excludeSt === "force" ? true : null,
        once_per_day: oncePerDay === "force" ? true : null,
        min_amount: parseOptionalNumber(minAmount),
        cooldown_seconds: parseOptionalNumber(cooldownSeconds),
      }),
    onSuccess: () => {
      onFlash("预警策略已保存");
      queryClient.invalidateQueries({ queryKey: ["alert-policy", workspaceId] });
    },
    onError: (error) => onFlash(`保存预警策略失败：${(error as Error).message}`),
  });

  const reset = useMutation({
    mutationFn: () =>
      api.updateAlertPolicy(workspaceId, {
        exclude_st: null,
        once_per_day: null,
        min_amount: null,
        cooldown_seconds: null,
      }),
    onSuccess: () => {
      setResetOpen(false);
      onFlash("预警策略已重置为跟随规则");
      queryClient.invalidateQueries({ queryKey: ["alert-policy", workspaceId] });
    },
    onError: (error) => {
      setResetOpen(false);
      onFlash(`重置预警策略失败：${(error as Error).message}`);
    },
  });

  return (
    <div className="card">
      <div className="mb-2">
        <h2 className="text-sm font-semibold text-slate-100">预警策略</h2>
        <p className="mt-1 text-xs text-slate-500">
          策略只收紧、不放宽：与规则自身设置取更严格者。未覆盖的项跟随规则自身设置；
          变更从下一次扫描起生效，历史触发的去重键不重写。
        </p>
      </div>

      {policy.isLoading && <p className="text-xs text-slate-500">预警策略加载中…</p>}
      {policy.isError && <p className="text-xs text-slate-500">预警策略暂不可用，不影响其他功能。</p>}

      {policy.data && (
        <div className="table-wrap">
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-800">
                <th className="th">生效参数</th>
                <th className="th">生效值</th>
              </tr>
            </thead>
            <tbody>
              <tr className="border-b border-slate-800/50">
                <td className="td text-slate-300">排除 ST</td>
                <td className="td text-slate-300">{policy.data.effective.exclude_st ? "排除" : "不排除"}</td>
              </tr>
              <tr className="border-b border-slate-800/50">
                <td className="td text-slate-300">单笔最低成交额</td>
                <td className="td text-slate-300">
                  {policy.data.effective.min_amount > 0
                    ? `${policy.data.effective.min_amount.toLocaleString()} 元`
                    : "不限"}
                </td>
              </tr>
              <tr className="border-b border-slate-800/50">
                <td className="td text-slate-300">冷却时间</td>
                <td className="td text-slate-300">
                  {policy.data.effective.cooldown_seconds > 0
                    ? `${policy.data.effective.cooldown_seconds} 秒`
                    : "不限制"}
                </td>
              </tr>
              <tr className="last:border-0">
                <td className="td text-slate-300">每日最多一次</td>
                <td className="td text-slate-300">{policy.data.effective.once_per_day ? "是" : "否"}</td>
              </tr>
            </tbody>
          </table>
        </div>
      )}

      {canManage && (
        <form
          className="mt-3 flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            save.mutate();
          }}
        >
          <div>
            <label className="label" htmlFor="policy-exclude-st">
              排除 ST
            </label>
            <select
              id="policy-exclude-st"
              className="input"
              value={excludeSt}
              onChange={(event) => setExcludeSt(event.target.value as "follow" | "force")}
            >
              <option value="follow">跟随规则</option>
              <option value="force">强制排除 ST</option>
            </select>
          </div>
          <div>
            <label className="label" htmlFor="policy-once-per-day">
              每日最多一次
            </label>
            <select
              id="policy-once-per-day"
              className="input"
              value={oncePerDay}
              onChange={(event) => setOncePerDay(event.target.value as "follow" | "force")}
            >
              <option value="follow">跟随规则</option>
              <option value="force">每天最多一次</option>
            </select>
          </div>
          <div>
            <label className="label" htmlFor="policy-min-amount">
              最低成交额（元）
            </label>
            <input
              id="policy-min-amount"
              className="input w-36"
              type="number"
              min={0}
              placeholder="留空=跟随规则"
              value={minAmount}
              onChange={(event) => setMinAmount(event.target.value)}
            />
          </div>
          <div>
            <label className="label" htmlFor="policy-cooldown">
              冷却时间（秒）
            </label>
            <input
              id="policy-cooldown"
              className="input w-36"
              type="number"
              min={0}
              max={30 * 24 * 3600}
              placeholder="留空=跟随规则"
              value={cooldownSeconds}
              onChange={(event) => setCooldownSeconds(event.target.value)}
            />
          </div>
          <button className="btn btn-primary" disabled={save.isPending}>
            {save.isPending ? "保存中…" : "保存策略"}
          </button>
          <button
            className="btn"
            type="button"
            disabled={reset.isPending}
            onClick={() => setResetOpen(true)}
          >
            重置为跟随规则
          </button>
        </form>
      )}

      {!canManage && (
        <p className="mt-3 text-xs text-slate-500">
          {adminViewing ? "管理员查看模式下预警策略只读。" : "只有所有者可以修改预警策略。"}
        </p>
      )}

      <ConfirmDialog
        open={resetOpen}
        title="重置预警策略"
        description="所有维度将恢复为「跟随规则」：排除 ST、最低成交额、冷却时间与每日一次都不再由工作区策略收紧。此操作不删除任何规则与触发记录。"
        confirmLabel="重置为跟随规则"
        danger
        busy={reset.isPending}
        onCancel={() => setResetOpen(false)}
        onConfirm={() => reset.mutate()}
      />
    </div>
  );
}
