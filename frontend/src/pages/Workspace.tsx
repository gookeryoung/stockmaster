/** 工作区成员与角色管理。
 *
 * 三个角色对应三种界面：
 *   owner  —— 全部可见可改（改名、加删成员、转让、删除）
 *   editor —— 只能看成员列表（他能写数据，但不能决定谁能进来）
 *   viewer —— 同上，另外页面顶部提示「只读」
 *
 * 界面按角色隐藏按钮，但**权限判断在服务端**：隐藏只是减少误点，不是安全边界。
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import {
  ROLE_LABEL,
  canManageWorkspace,
  canWriteWorkspace,
  useCurrentWorkspace,
  useWorkspace,
} from "../workspace";
import type { WorkspaceRole } from "../types";

const ROLE_HINT: Record<WorkspaceRole, string> = {
  owner: "可改工作区信息、管理成员、转让所有权",
  editor: "可增删自选、预警规则与回测",
  viewer: "只读，不能修改任何数据",
};

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
  const [message, setMessage] = useState<string | null>(null);

  const canManage = canManageWorkspace(current);
  const canWrite = canWriteWorkspace(current);
  const workspaceId = current?.id ?? null;

  const members = useQuery({
    queryKey: ["workspace-members", workspaceId],
    queryFn: () => api.workspaceMembers(workspaceId as number),
    enabled: workspaceId !== null,
  });

  const flash = (text: string) => setMessage(text);

  const createWorkspace = useMutation({
    mutationFn: () => api.createWorkspace({ name: newName.trim(), description: newDesc.trim() }),
    onSuccess: async (created) => {
      setNewName("");
      setNewDesc("");
      await refresh();
      select(created.id);
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

  const updateInfo = useMutation({
    mutationFn: () => api.updateWorkspace(workspaceId as number, { name: rename.trim() }),
    onSuccess: async () => {
      flash("已更新工作区名称");
      setRename("");
      await refresh();
    },
    onError: (error) => flash(`改名失败：${(error as Error).message}`),
  });

  const deleteWorkspace = useMutation({
    mutationFn: (purge: boolean) => api.deleteWorkspace(workspaceId as number, purge),
    onSuccess: async () => {
      flash("工作区已删除");
      await refresh();
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
