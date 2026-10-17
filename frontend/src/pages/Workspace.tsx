/** 工作区成员与角色管理。
 *
 * 三个角色对应三种界面：
 *   owner  —— 全部可见可改（改名、加删成员、转让、删除）
 *   editor —— 只能看成员列表（他能写数据，但不能决定谁能进来）
 *   viewer —— 同上，另外页面顶部提示「只读」
 *
 * 界面按角色隐藏按钮，但**权限判断在服务端**：隐藏只是减少误点，不是安全边界。
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import { formatDateTime } from "../format";
import {
  ROLE_LABEL,
  canManageWorkspace,
  canWriteWorkspace,
  useCurrentWorkspace,
  useWorkspace,
} from "../workspace";
import type { WorkspaceChannelKind, WorkspaceRole, WorkspaceUsage } from "../types";

const ROLE_HINT: Record<WorkspaceRole, string> = {
  owner: "可改工作区信息、管理成员、转让所有权",
  editor: "可增删自选、预警规则与回测",
  viewer: "只读，不能修改任何数据",
};

/** 用量与配额表：只读，所有成员可见。
 *
 * `limits[key] === 0` 表示该资源不限量；达到上限时用红色 badge 提示，
 * 但不阻止任何操作（真正的拦截在服务端，且以服务端返回的错误为准）。
 */
function UsageTable({ data }: { data: WorkspaceUsage }) {
  return (
    <>
      <div className="table-wrap">
        <table className="w-full">
          <thead>
            <tr className="border-b border-slate-800">
              <th className="th">资源</th>
              <th className="th">用量</th>
              <th className="th">上限</th>
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
                  <td className="td text-slate-300">{used}</td>
                  <td className="td text-slate-400">{limit > 0 ? limit : "不限"}</td>
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
        合计 {data.total} 项用量（含成员与通道） · 配额由管理员通过 SM_WORKSPACE_MAX_* 配置，默认不限；隔离关闭时不生效。
      </p>
    </>
  );
}

/** 工作区级预警策略：成员只读「生效值」，owner 可覆盖。
 *
 * 四个字段为 null 表示「跟随规则」；策略只收紧、不放宽，所以两个布尔项只有
 * 「跟随规则 / 强制开启」两种选择。空数字输入同样表示跟随规则。
 */
function AlertPolicySection({
  workspaceId,
  canManage,
  onFlash,
}: {
  workspaceId: number;
  canManage: boolean;
  onFlash: (text: string) => void;
}) {
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
      onFlash("预警策略已重置为跟随规则");
      queryClient.invalidateQueries({ queryKey: ["alert-policy", workspaceId] });
    },
    onError: (error) => onFlash(`重置预警策略失败：${(error as Error).message}`),
  });

  return (
    <section className="card">
      <div className="mb-2">
        <h2 className="text-sm font-semibold text-slate-100">预警策略</h2>
        <p className="mt-1 text-xs text-slate-500">
          策略只收紧、不放宽：与规则自身设置取更严格者。未覆盖的项跟随规则自身设置。
        </p>
      </div>

      {policy.isLoading && <p className="text-xs text-slate-500">预警策略加载中…</p>}
      {policy.isError && (
        <p className="text-xs text-slate-500">预警策略暂不可用，不影响其他功能。</p>
      )}

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
                <td className="td text-slate-300">
                  {policy.data.effective.exclude_st ? "排除" : "不排除"}
                </td>
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
                <td className="td text-slate-300">
                  {policy.data.effective.once_per_day ? "是" : "否"}
                </td>
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
            <label className="label">排除 ST</label>
            <select
              className="input"
              value={excludeSt}
              onChange={(event) => setExcludeSt(event.target.value as "follow" | "force")}
            >
              <option value="follow">跟随规则</option>
              <option value="force">强制排除 ST</option>
            </select>
          </div>
          <div>
            <label className="label">每日最多一次</label>
            <select
              className="input"
              value={oncePerDay}
              onChange={(event) => setOncePerDay(event.target.value as "follow" | "force")}
            >
              <option value="follow">跟随规则</option>
              <option value="force">每天最多一次</option>
            </select>
          </div>
          <div>
            <label className="label">最低成交额（元）</label>
            <input
              className="input w-36"
              type="number"
              min={0}
              placeholder="留空=跟随规则"
              value={minAmount}
              onChange={(event) => setMinAmount(event.target.value)}
            />
          </div>
          <div>
            <label className="label">冷却时间（秒）</label>
            <input
              className="input w-36"
              type="number"
              min={0}
              placeholder="留空=跟随规则"
              value={cooldownSeconds}
              onChange={(event) => setCooldownSeconds(event.target.value)}
            />
          </div>
          <button className="btn btn-primary" disabled={save.isPending}>
            保存
          </button>
          <button
            className="btn"
            type="button"
            disabled={reset.isPending}
            onClick={() => {
              if (window.confirm("重置为跟随规则？")) reset.mutate();
            }}
          >
            重置
          </button>
        </form>
      )}

      {!canManage && (
        <p className="mt-3 text-xs text-slate-500">只有所有者可以修改预警策略。</p>
      )}
    </section>
  );
}

export default function WorkspacePage() {
  const queryClient = useQueryClient();
  const user = useAuth((state) => state.user);
  const config = useAuth((state) => state.config);
  const list = useWorkspace((state) => state.list);
  const current = useCurrentWorkspace();
  const refresh = useWorkspace((state) => state.refresh);
  const select = useWorkspace((state) => state.select);

  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [memberName, setMemberName] = useState("");
  const [memberRole, setMemberRole] = useState<WorkspaceRole>("viewer");
  const [rename, setRename] = useState("");
  const [channelName, setChannelName] = useState("");
  const [channelKind, setChannelKind] = useState<WorkspaceChannelKind>("webhook");
  const [channelUrl, setChannelUrl] = useState("");
  const [channelEnabled, setChannelEnabled] = useState(true);
  const [message, setMessage] = useState<string | null>(null);

  const canManage = canManageWorkspace(current);
  const canWrite = canWriteWorkspace(current);
  const isAdmin = user?.role === "admin";
  const workspaceId = current?.id ?? null;

  const members = useQuery({
    queryKey: ["workspace-members", workspaceId],
    queryFn: () => api.workspaceMembers(workspaceId as number),
    enabled: workspaceId !== null,
  });

  const channels = useQuery({
    queryKey: ["workspace-channels", workspaceId],
    queryFn: () => api.workspaceChannels(workspaceId as number),
    enabled: workspaceId !== null,
  });

  // 用量与配额：失败只在这张卡片里提示，不影响页面其它区块。
  const usage = useQuery({
    queryKey: ["workspace-usage", workspaceId],
    queryFn: () => api.workspaceStats(workspaceId as number),
    enabled: workspaceId !== null,
    retry: false,
  });

  // 管理员总览：后端对非管理员会退回「自己的列表」，这里只在管理员界面查询。
  const allWorkspaces = useQuery({
    queryKey: ["workspaces-all"],
    queryFn: () => api.workspaces(true),
    enabled: isAdmin && Boolean(config?.workspace_enabled),
    retry: false,
  });

  const flash = (text: string) => setMessage(text);

  const createWorkspace = useMutation({
    mutationFn: () => api.createWorkspace({ name: newName.trim(), description: newDesc.trim() }),
    onSuccess: async (created) => {
      setNewName("");
      setNewDesc("");
      await refresh();
      select(created.id);
      queryClient.invalidateQueries({ queryKey: ["workspaces-all"] });
      flash(`已创建「${created.name}」`);
    },
    onError: (error) => flash(`创建失败：${(error as Error).message}`),
  });

  const addMember = useMutation({
    mutationFn: () => api.addWorkspaceMember(workspaceId as number, memberName.trim(), memberRole),
    onSuccess: (member) => {
      setMemberName("");
      flash(`已加入 ${member.username}（${ROLE_LABEL[member.role]}）`);
      queryClient.invalidateQueries({ queryKey: ["workspace-members", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-usage", workspaceId] });
      void refresh();
    },
    onError: (error) => flash(`添加失败：${(error as Error).message}`),
  });

  const changeRole = useMutation({
    mutationFn: ({ userId, role }: { userId: number; role: WorkspaceRole }) =>
      api.updateWorkspaceMember(workspaceId as number, userId, role),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["workspace-members", workspaceId] }),
    onError: (error) => flash(`改角色失败：${(error as Error).message}`),
  });

  const removeMember = useMutation({
    mutationFn: (userId: number) => api.removeWorkspaceMember(workspaceId as number, userId),
    onSuccess: () => {
      flash("已移除成员");
      queryClient.invalidateQueries({ queryKey: ["workspace-members", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-usage", workspaceId] });
      void refresh();
    },
    onError: (error) => flash(`移除失败：${(error as Error).message}`),
  });

  const transfer = useMutation({
    mutationFn: (userId: number) => api.transferWorkspace(workspaceId as number, userId),
    onSuccess: async () => {
      flash("已转让所有权，你现在的角色是编辑者");
      await refresh();
      queryClient.invalidateQueries({ queryKey: ["workspace-members", workspaceId] });
    },
    onError: (error) => flash(`转让失败：${(error as Error).message}`),
  });

  // 通知通道：读对所有成员开放（地址已脱敏），写操作只出现在 owner 的界面上，
  // 真正的权限校验在服务端。
  const createChannel = useMutation({
    mutationFn: () =>
      api.createWorkspaceChannel(workspaceId as number, {
        name: channelName.trim(),
        kind: channelKind,
        url: channelUrl.trim(),
        enabled: channelEnabled,
      }),
    onSuccess: (channel) => {
      setChannelName("");
      setChannelUrl("");
      setChannelEnabled(true);
      flash(`已添加通道「${channel.name}」`);
      queryClient.invalidateQueries({ queryKey: ["workspace-channels", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-usage", workspaceId] });
    },
    onError: (error) => flash(`添加通道失败：${(error as Error).message}`),
  });

  const toggleChannel = useMutation({
    mutationFn: ({ channelId, enabled }: { channelId: number; enabled: boolean }) =>
      api.updateWorkspaceChannel(workspaceId as number, channelId, { enabled }),
    onSuccess: (channel) => {
      flash(channel.enabled ? `已启用通道「${channel.name}」` : `已停用通道「${channel.name}」`);
      queryClient.invalidateQueries({ queryKey: ["workspace-channels", workspaceId] });
    },
    onError: (error) => flash(`切换通道状态失败：${(error as Error).message}`),
  });

  const testChannel = useMutation({
    mutationFn: (channelId: number) => api.testWorkspaceChannel(workspaceId as number, channelId),
    onSuccess: (result) => {
      flash(result.delivered ? `已发送测试通知（${result.channel}）` : "测试失败：通道未送达");
    },
    onError: (error) => flash(`测试失败：${(error as Error).message}`),
  });

  const deleteChannel = useMutation({
    mutationFn: (channelId: number) => api.deleteWorkspaceChannel(workspaceId as number, channelId),
    onSuccess: () => {
      flash("已删除通道");
      queryClient.invalidateQueries({ queryKey: ["workspace-channels", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workspace-usage", workspaceId] });
    },
    onError: (error) => flash(`删除通道失败：${(error as Error).message}`),
  });

  const updateInfo = useMutation({
    mutationFn: () => api.updateWorkspace(workspaceId as number, { name: rename.trim() }),
    onSuccess: async () => {
      flash("已更新工作区名称");
      setRename("");
      await refresh();
      queryClient.invalidateQueries({ queryKey: ["workspaces-all"] });
    },
    onError: (error) => flash(`改名失败：${(error as Error).message}`),
  });

  const deleteWorkspace = useMutation({
    mutationFn: (purge: boolean) => api.deleteWorkspace(workspaceId as number, purge),
    onSuccess: async () => {
      flash("工作区已删除");
      await refresh();
      queryClient.invalidateQueries({ queryKey: ["workspaces-all"] });
    },
    onError: (error) => flash(`删除失败：${(error as Error).message}（清空数据需确认 purge）`),
  });

  if (!config?.workspace_enabled) {
    return (
      <div className="card text-sm text-slate-400">
        当前未启用数据隔离（SM_DATA_ISOLATION=false），所有人共享同一份数据，因此没有工作区概念。
      </div>
    );
  }

  if (!user) {
    return <div className="card text-sm text-slate-400">请先登录后再管理工区。</div>;
  }

  return (
    <div className="space-y-3">
      {message && (
        <div className="flex items-start gap-2 rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-xs text-slate-300">
          <span className="flex-1">{message}</span>
          <button className="text-slate-500 hover:text-slate-300" onClick={() => setMessage(null)}>
            关闭
          </button>
        </div>
      )}

      {!canWrite && (
        <div className="rounded-md border border-amber-800/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-300">
          你在「{current?.name}」是查看者：可以阅读这里的自选、预警与回测，但不能修改。
        </div>
      )}

      {/* 我的工作区 */}
      <section className="card">
        <div className="mb-2 flex items-baseline justify-between">
          <h2 className="text-sm font-semibold text-slate-100">我的工作区</h2>
          <span className="text-xs text-slate-500">
            共 {list.length} 个 · 切换后全站数据随之切换
          </span>
        </div>
        <div className="table-wrap">
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-800">
                <th className="th">名称</th>
                <th className="th">类型</th>
                <th className="th">我的角色</th>
                <th className="th">成员</th>
                <th className="th" />
              </tr>
            </thead>
            <tbody>
              {list.map((row) => (
                <tr key={row.id} className="border-b border-slate-800/50 last:border-0">
                  <td className="td">
                    <span className="font-medium text-slate-100">{row.name}</span>
                    {row.description && <span className="ml-2 text-xs text-slate-500">{row.description}</span>}
                  </td>
                  <td className="td text-slate-400">{row.kind === "personal" ? "个人" : "共享"}</td>
                  <td className="td">
                    <span
                      className={`badge ${
                        row.role === "viewer"
                          ? "bg-amber-900/50 text-amber-300"
                          : row.role === "owner"
                            ? "bg-emerald-900/50 text-emerald-300"
                            : "bg-sky-900/50 text-sky-300"
                      }`}
                    >
                      {ROLE_LABEL[row.role] ?? row.role}
                    </span>
                  </td>
                  <td className="td text-slate-400">{row.member_count}</td>
                  <td className="td text-right">
                    {row.id === workspaceId ? (
                      <span className="text-xs text-slate-500">当前</span>
                    ) : (
                      <button className="btn px-2 py-1 text-xs" onClick={() => select(row.id)}>
                        切换
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {list.length === 0 && (
                <tr>
                  <td className="td text-slate-500" colSpan={5}>
                    还没有工作区。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        <form
          className="mt-3 flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (newName.trim()) createWorkspace.mutate();
          }}
        >
          <div>
            <label className="label">新建共享工作区</label>
            <input
              className="input w-48"
              placeholder="例如：投研小组"
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
              maxLength={64}
            />
          </div>
          <div>
            <label className="label">描述（可选）</label>
            <input
              className="input w-56"
              value={newDesc}
              onChange={(event) => setNewDesc(event.target.value)}
              maxLength={256}
            />
          </div>
          <button className="btn btn-primary" disabled={!newName.trim() || createWorkspace.isPending}>
            创建
          </button>
        </form>
      </section>

      {/* 管理员：全部工作区总览（只读） */}
      {isAdmin && (
        <section className="card">
          <div className="mb-2 flex items-baseline justify-between">
            <h2 className="text-sm font-semibold text-slate-100">全部工作区（管理员）</h2>
            <span className="text-xs text-slate-500">只读 · 含个人与共享工作区</span>
          </div>
          {allWorkspaces.isLoading && <p className="text-xs text-slate-500">加载中…</p>}
          {allWorkspaces.isError && (
            <p className="text-xs text-slate-500">全部工作区暂不可用。</p>
          )}
          {allWorkspaces.data && (
            <div className="table-wrap">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-slate-800">
                    <th className="th">名称</th>
                    <th className="th">类型</th>
                    <th className="th">所有者 ID</th>
                    <th className="th">成员</th>
                    <th className="th">数据量</th>
                    <th className="th">创建时间</th>
                  </tr>
                </thead>
                <tbody>
                  {allWorkspaces.data.map((row) => (
                    <tr key={row.id} className="border-b border-slate-800/50 last:border-0">
                      <td className="td">
                        <span className="font-medium text-slate-100">{row.name}</span>
                        {row.description && (
                          <span className="ml-2 text-xs text-slate-500">{row.description}</span>
                        )}
                      </td>
                      <td className="td text-slate-400">
                        {row.kind === "personal" ? "个人" : "共享"}
                      </td>
                      <td className="td text-slate-500">{row.owner_id ?? "—"}</td>
                      <td className="td text-slate-400">{row.member_count}</td>
                      <td className="td text-slate-300">{row.data_total} 条</td>
                      <td className="td text-xs text-slate-400">
                        {formatDateTime(row.created_at)}
                      </td>
                    </tr>
                  ))}
                  {allWorkspaces.data.length === 0 && (
                    <tr>
                      <td className="td text-slate-500" colSpan={6}>
                        还没有任何工作区。
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          )}
        </section>
      )}

      {/* 用量与配额（只读，所有成员可见） */}
      <section className="card">
        <div className="mb-2 flex items-baseline justify-between">
          <h2 className="text-sm font-semibold text-slate-100">
            用量与配额 · {current?.name ?? "未选择"}
          </h2>
          <span className="text-xs text-slate-500">只读 · 达到上限时相关写入会被服务端拒绝</span>
        </div>
        {usage.isLoading && <p className="text-xs text-slate-500">用量加载中…</p>}
        {usage.isError && (
          <p className="text-xs text-slate-500">用量信息暂不可用，不影响页面其他操作。</p>
        )}
        {usage.data && <UsageTable data={usage.data} />}
      </section>

      {/* 当前工作区成员 */}
      <section className="card">
        <div className="mb-2 flex items-baseline justify-between">
          <h2 className="text-sm font-semibold text-slate-100">
            成员 · {current?.name ?? "未选择"}
          </h2>
          <span className="text-xs text-slate-500">
            {current ? ROLE_HINT[current.role] : ""}
          </span>
        </div>

        <div className="table-wrap">
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-800">
                <th className="th">账号</th>
                <th className="th">昵称</th>
                <th className="th">角色</th>
                <th className="th" />
              </tr>
            </thead>
            <tbody>
              {(members.data ?? []).map((row) => (
                <tr key={row.user_id} className="border-b border-slate-800/50 last:border-0">
                  <td className="td font-medium text-slate-100">{row.username || `user#${row.user_id}`}</td>
                  <td className="td text-slate-400">{row.nickname || "—"}</td>
                  <td className="td">
                    {canManage && row.user_id !== user.id ? (
                      <select
                        className="input py-1 text-xs"
                        value={row.role}
                        onChange={(event) =>
                          changeRole.mutate({ userId: row.user_id, role: event.target.value as WorkspaceRole })
                        }
                      >
                        <option value="owner">所有者</option>
                        <option value="editor">编辑者</option>
                        <option value="viewer">查看者</option>
                      </select>
                    ) : (
                      <span className="text-slate-300">{ROLE_LABEL[row.role] ?? row.role}</span>
                    )}
                  </td>
                  <td className="td text-right">
                    {canManage && row.user_id !== user.id && (
                      <div className="flex justify-end gap-1">
                        {row.role !== "owner" && (
                          <button
                            className="btn px-2 py-1 text-xs"
                            onClick={() => transfer.mutate(row.user_id)}
                            title="把所有权转给此人（自己降为编辑者）"
                          >
                            转让
                          </button>
                        )}
                        <button className="btn px-2 py-1 text-xs" onClick={() => removeMember.mutate(row.user_id)}>
                          移除
                        </button>
                      </div>
                    )}
                    {row.user_id === user.id && <span className="text-xs text-slate-500">我</span>}
                  </td>
                </tr>
              ))}
              {members.isLoading && (
                <tr>
                  <td className="td text-slate-500" colSpan={4}>
                    加载中…
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {canManage && (
          <form
            className="mt-3 flex flex-wrap items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (memberName.trim()) addMember.mutate();
            }}
          >
            <div>
              <label className="label">按用户名添加</label>
              <input
                className="input w-48"
                placeholder="账号名"
                value={memberName}
                onChange={(event) => setMemberName(event.target.value)}
              />
            </div>
            <div>
              <label className="label">角色</label>
              <select
                className="input"
                value={memberRole}
                onChange={(event) => setMemberRole(event.target.value as WorkspaceRole)}
              >
                <option value="viewer">查看者（只读）</option>
                <option value="editor">编辑者（可改数据）</option>
              </select>
            </div>
            <button className="btn btn-primary" disabled={!memberName.trim() || addMember.isPending}>
              添加
            </button>
          </form>
        )}

        {!canManage && (
          <p className="mt-3 text-xs text-slate-500">
            只有所有者可以增删成员或调整角色。需要变更请让「{current?.name}」的所有者操作。
          </p>
        )}
      </section>

      {/* 通知通道：本工作区的预警外发目标 */}
      <section className="card">
        <div className="mb-2">
          <h2 className="text-sm font-semibold text-slate-100">通知通道</h2>
          <p className="mt-1 text-xs text-slate-500">
            通道只接收当前工作区的预警；服务端保存后地址仅以脱敏形式展示。
            {canManage ? "启用、测试、删除与新增均由你管理。" : "只有所有者可以增删改通道或发送测试通知。"}
          </p>
        </div>

        <div className="table-wrap">
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-800">
                <th className="th">名称</th>
                <th className="th">类型</th>
                <th className="th">地址（脱敏）</th>
                <th className="th">状态</th>
                {canManage && <th className="th" />}
              </tr>
            </thead>
            <tbody>
              {(channels.data ?? []).map((row) => (
                <tr key={row.id} className="border-b border-slate-800/50 last:border-0">
                  <td className="td font-medium text-slate-100">{row.name}</td>
                  <td className="td text-slate-400">{row.kind === "bark" ? "Bark" : "Webhook"}</td>
                  <td className="td font-mono text-xs text-slate-500">{row.url || "—"}</td>
                  <td className="td">
                    <span
                      className={`badge ${
                        row.enabled
                          ? "bg-emerald-900/50 text-emerald-300"
                          : "bg-slate-800 text-slate-400"
                      }`}
                    >
                      {row.enabled ? "启用" : "停用"}
                    </span>
                  </td>
                  {canManage && (
                    <td className="td text-right">
                      <div className="flex justify-end gap-1">
                        <button
                          className="btn px-2 py-1 text-xs"
                          disabled={toggleChannel.isPending}
                          onClick={() => toggleChannel.mutate({ channelId: row.id, enabled: !row.enabled })}
                        >
                          {row.enabled ? "停用" : "启用"}
                        </button>
                        <button
                          className="btn px-2 py-1 text-xs"
                          disabled={testChannel.isPending}
                          onClick={() => testChannel.mutate(row.id)}
                          title="向该通道发送一条测试通知"
                        >
                          测试
                        </button>
                        <button
                          className="btn px-2 py-1 text-xs"
                          disabled={deleteChannel.isPending}
                          onClick={() => {
                            if (window.confirm(`确定删除通道「${row.name}」？`)) {
                              deleteChannel.mutate(row.id);
                            }
                          }}
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  )}
                </tr>
              ))}
              {channels.isLoading && (
                <tr>
                  <td className="td text-slate-500" colSpan={canManage ? 5 : 4}>
                    加载中…
                  </td>
                </tr>
              )}
              {!channels.isLoading && (channels.data ?? []).length === 0 && (
                <tr>
                  <td className="td text-slate-500" colSpan={canManage ? 5 : 4}>
                    尚未配置通道，本工作区触发预警时不会外发。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {canManage && (
          <form
            className="mt-3 flex flex-wrap items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (channelName.trim() && channelUrl.trim()) createChannel.mutate();
            }}
          >
            <div>
              <label className="label">名称</label>
              <input
                className="input w-40"
                placeholder="例如：投研群机器人"
                value={channelName}
                onChange={(event) => setChannelName(event.target.value)}
                maxLength={64}
              />
            </div>
            <div>
              <label className="label">类型</label>
              <select
                className="input"
                value={channelKind}
                onChange={(event) => setChannelKind(event.target.value as WorkspaceChannelKind)}
              >
                <option value="webhook">Webhook</option>
                <option value="bark">Bark</option>
              </select>
            </div>
            <div>
              <label className="label">URL</label>
              <input
                className="input w-96"
                placeholder={
                  channelKind === "bark"
                    ? "https://api.day.app/你的Key"
                    : "https://oapi.dingtalk.com/robot/send?access_token=..."
                }
                value={channelUrl}
                onChange={(event) => setChannelUrl(event.target.value)}
                maxLength={512}
              />
            </div>
            <label className="flex items-center gap-2 pb-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={channelEnabled}
                onChange={(event) => setChannelEnabled(event.target.checked)}
              />
              启用
            </label>
            <button
              className="btn btn-primary"
              disabled={!channelName.trim() || !channelUrl.trim() || createChannel.isPending}
            >
              添加通道
            </button>
          </form>
        )}
      </section>

      {/* 预警策略（成员只读生效值，owner 可覆盖） */}
      {current && (
        <AlertPolicySection workspaceId={current.id} canManage={canManage} onFlash={flash} />
      )}

      {/* 工作区设置（仅 owner） */}
      {canManage && current && (
        <section className="card space-y-3">
          <h2 className="text-sm font-semibold text-slate-100">工作区设置</h2>
          <form
            className="flex flex-wrap items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (rename.trim()) updateInfo.mutate();
            }}
          >
            <div>
              <label className="label">重命名</label>
              <input
                className="input w-56"
                placeholder={current.name}
                value={rename}
                onChange={(event) => setRename(event.target.value)}
                maxLength={64}
              />
            </div>
            <button className="btn" disabled={!rename.trim() || updateInfo.isPending}>
              保存
            </button>
          </form>

          <div className="flex flex-wrap items-center gap-2 border-t border-slate-800 pt-3">
            <span className="text-xs text-slate-500">
              {current.kind === "personal"
                ? "个人工作区不可删除（它是你数据的落脚点），可以改名或分享给他人。"
                : "删除会连同工作区里的自选、预警与回测一起清空，且不可撤销。"}
            </span>
            <button
              className="btn ml-auto border-rose-800 text-rose-300 hover:border-rose-600 hover:bg-rose-950/40"
              disabled={current.kind === "personal" || deleteWorkspace.isPending}
              onClick={() => {
                const first = window.confirm(`确定删除「${current.name}」？工作区里的数据会一并删除。`);
                if (first) deleteWorkspace.mutate(true);
              }}
            >
              删除工作区
            </button>
          </div>
        </section>
      )}
    </div>
  );
}
