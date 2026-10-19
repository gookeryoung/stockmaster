/** 工作区概览：身份摘要、owner 改名、我的工作区列表、危险区。
 *
 * 数据来自父组件的 `GET /workspaces` 列表与成员查询；写操作仍走
 * `api.updateWorkspace` / `api.deleteWorkspace`，成功后 refresh 列表让切换器同步。
 * 危险操作用统一 ConfirmDialog，删除需要勾选“我已了解”才能确认。
 */

import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api";
import ConfirmDialog from "../../components/ConfirmDialog";
import { ROLE_LABEL, useWorkspace } from "../../workspace";
import { formatDateTime } from "../../format";
import type { Workspace, WorkspaceMember } from "../../types";

interface Props {
  current: Workspace;
  list: Workspace[];
  currentId: number | null;
  members: WorkspaceMember[] | undefined;
  membersLoading: boolean;
  canManage: boolean;
  /** 管理员跨区查看模式：只读并显示明确标识。 */
  adminViewing?: boolean;
  onFlash: (text: string) => void;
}

export default function OverviewSection({
  current,
  list,
  currentId,
  members,
  membersLoading,
  canManage,
  adminViewing = false,
  onFlash,
}: Props) {
  const queryClient = useQueryClient();
  const refresh = useWorkspace((state) => state.refresh);
  const select = useWorkspace((state) => state.select);

  const [rename, setRename] = useState("");
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [deleteOpen, setDeleteOpen] = useState(false);

  const owners = (members ?? []).filter((member) => member.role === "owner");
  const ownerLabel = membersLoading
    ? "加载中…"
    : owners.length === 0
      ? "—"
      : owners.length === 1
        ? owners[0].username || `user#${owners[0].user_id}`
        : `${owners[0].username || `user#${owners[0].user_id}`} 等 ${owners.length} 人`;

  const updateInfo = useMutation({
    mutationFn: () => api.updateWorkspace(current.id, { name: rename.trim() }),
    onSuccess: async () => {
      onFlash("已更新工作区名称");
      setRename("");
      await refresh();
      queryClient.invalidateQueries({ queryKey: ["workspaces-all"] });
    },
    onError: (error) => onFlash(`改名失败：${(error as Error).message}`),
  });

  const createWorkspace = useMutation({
    mutationFn: () => api.createWorkspace({ name: newName.trim(), description: newDesc.trim() }),
    onSuccess: async (created) => {
      setNewName("");
      setNewDesc("");
      await refresh();
      select(created.id);
      queryClient.invalidateQueries({ queryKey: ["workspaces-all"] });
      onFlash(`已创建「${created.name}」`);
    },
    onError: (error) => onFlash(`创建失败：${(error as Error).message}`),
  });

  const deleteWorkspace = useMutation({
    mutationFn: () => api.deleteWorkspace(current.id, true),
    onSuccess: async () => {
      setDeleteOpen(false);
      onFlash(`已删除工作区「${current.name}」`);
      await refresh();
      queryClient.invalidateQueries({ queryKey: ["workspaces-all"] });
    },
    onError: (error) => {
      setDeleteOpen(false);
      onFlash(`删除失败：${(error as Error).message}`);
    },
  });

  return (
    <section id="overview" className="scroll-mt-16 space-y-3">
      <div className="card">
        {adminViewing && (
          <div className="mb-2 rounded-md border border-amber-800/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-300">
            管理员查看模式：你不是该工作区成员，仅用于排障/支持。此页面一律只读，写操作已禁用。
          </div>
        )}
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="text-base font-semibold text-slate-100">{current.name}</h2>
            <span className={`badge ${current.kind === "personal" ? "bg-slate-800 text-slate-300" : "bg-sky-900/50 text-sky-300"}`}>
              {current.kind === "personal" ? "个人" : "共享"}
            </span>
            {adminViewing ? (
              <span className="badge bg-amber-900/50 text-amber-300">管理员查看</span>
            ) : (
              <span
                className={`badge ${
                  current.role === "viewer"
                    ? "bg-amber-900/50 text-amber-300"
                    : current.role === "owner"
                      ? "bg-emerald-900/50 text-emerald-300"
                      : "bg-sky-900/50 text-sky-300"
                }`}
              >
                {ROLE_LABEL[current.role] ?? current.role}
              </span>
            )}
          </div>
          {canManage && (
            <form
              className="flex items-end gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                if (rename.trim() && rename.trim() !== current.name) updateInfo.mutate();
              }}
            >
              <div>
                <label className="label" htmlFor="workspace-rename">
                  重命名
                </label>
                <input
                  id="workspace-rename"
                  className="input w-52"
                  placeholder={current.name}
                  value={rename}
                  onChange={(event) => setRename(event.target.value)}
                  maxLength={64}
                />
              </div>
              <button
                className="btn"
                disabled={!rename.trim() || rename.trim() === current.name || updateInfo.isPending}
              >
                {updateInfo.isPending ? "保存中…" : "保存"}
              </button>
            </form>
          )}
        </div>

        <dl className="grid grid-cols-1 gap-x-6 gap-y-1 text-sm sm:grid-cols-2 lg:grid-cols-4">
          <div className="flex gap-2">
            <dt className="text-slate-500">所有者</dt>
            <dd className="text-slate-300">{ownerLabel}</dd>
          </div>
          <div className="flex gap-2">
            <dt className="text-slate-500">成员</dt>
            <dd className="text-slate-300">{current.member_count} 人</dd>
          </div>
          <div className="flex gap-2">
            <dt className="text-slate-500">数据量</dt>
            <dd className="text-slate-300">{current.data_total} 条</dd>
          </div>
          <div className="flex gap-2">
            <dt className="text-slate-500">创建时间</dt>
            <dd className="text-slate-300">{formatDateTime(current.created_at)}</dd>
          </div>
        </dl>
        {current.description && (
          <p className="mt-1 text-xs text-slate-500" title={current.description}>
            描述：{current.description}
          </p>
        )}
        <p className="mt-1 text-xs text-slate-500">
          数据量口径：自选 + 预警规则 + 回测，不含成员与通知通道。
        </p>

        <div className="mt-3 border-t border-slate-800 pt-3">
          {adminViewing ? (
            <p className="text-xs text-amber-300/90">
              管理员查看模式下不提供改名、删除或成员管理；请退出查看后再以成员身份操作。
            </p>
          ) : canManage && current.kind === "shared" ? (
            <div className="flex flex-wrap items-center gap-2">
              <p className="flex-1 text-xs text-slate-500">
                危险区：删除会同时清空本工作区的自选、预警规则、回测与通知通道，
                且不可撤销；数据不会并入个人工作区。
              </p>
              <button
                className="btn border-rose-800 text-rose-300 hover:border-rose-600 hover:bg-rose-950/40"
                onClick={() => setDeleteOpen(true)}
              >
                删除工作区
              </button>
            </div>
          ) : (
            <p className="text-xs text-slate-500">
              {current.kind === "personal"
                ? "个人工作区不可删除（它是你数据的落脚点），可以改名或分享给他人。"
                : "只有所有者可以修改工作区设置或删除工作区。"}
            </p>
          )}
        </div>
      </div>

      <ConfirmDialog
        open={deleteOpen}
        title={`删除工作区「${current.name}」`}
        description={
          <>
            本工作区现有 {current.data_total} 条数据（自选、预警规则、回测）与全部通知通道。
            删除会将这些数据连同工作区一起清空，<strong className="text-rose-300">不可恢复</strong>，
            且数据不会并入个人工作区。
          </>
        }
        confirmLabel="删除工作区并清空数据"
        danger
        requireAcknowledge
        acknowledgeLabel="我已了解删除会永久清空本工作区的全部数据"
        busy={deleteWorkspace.isPending}
        onCancel={() => setDeleteOpen(false)}
        onConfirm={() => deleteWorkspace.mutate()}
      />

      <div className="card">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <h2 className="text-sm font-semibold text-slate-100">我的工作区</h2>
          <span className="text-xs text-slate-500">共 {list.length} 个 · 切换后全站数据随之切换</span>
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
                    {adminViewing && row.id === currentId ? (
                      <span className="badge bg-amber-900/50 text-amber-300">管理员查看</span>
                    ) : (
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
                    )}
                  </td>
                  <td className="td text-slate-400">{row.member_count}</td>
                  <td className="td text-right">
                    {row.id === currentId ? (
                      <span className="text-xs text-slate-500">当前</span>
                    ) : (
                      <button
                        className="btn px-2 py-1 text-xs"
                        title="切换后全站数据随之切换"
                        onClick={() => select(row.id)}
                      >
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

        {!adminViewing && (
          <form
            className="mt-3 flex flex-wrap items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (newName.trim()) createWorkspace.mutate();
            }}
          >
            <div>
              <label className="label" htmlFor="new-workspace-name">
                新建共享工作区
              </label>
              <input
                id="new-workspace-name"
                className="input w-48"
                placeholder="例如：投研小组"
                value={newName}
                onChange={(event) => setNewName(event.target.value)}
                maxLength={64}
              />
            </div>
            <div>
              <label className="label" htmlFor="new-workspace-desc">
                描述（可选）
              </label>
              <input
                id="new-workspace-desc"
                className="input w-56"
                value={newDesc}
                onChange={(event) => setNewDesc(event.target.value)}
                maxLength={256}
              />
            </div>
            <button className="btn btn-primary" disabled={!newName.trim() || createWorkspace.isPending}>
              {createWorkspace.isPending ? "创建中…" : "创建"}
            </button>
            <p className="w-full text-xs text-slate-500">
              创建者自动成为所有者；个人工作区已自动存在，无需创建。
            </p>
          </form>
        )}
      </div>
    </section>
  );
}
