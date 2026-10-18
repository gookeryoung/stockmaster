/** 成员管理：列表、改角色、移除、转让、按候选账号邀请。
 *
 * 邀请从“手输用户名”改成候选搜索：后端 `GET /workspaces/{id}/members/candidates`
 * 已存在（排除现有成员与非 active 账号），前端做 300ms 防抖。权限仍在服务端：
 * 前端隐藏只是减少误触，403/400 由服务端兜底。
 */

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api";
import { useAuth } from "../../auth";
import ConfirmDialog from "../../components/ConfirmDialog";
import { ROLE_LABEL, useWorkspace } from "../../workspace";
import { formatDateTime } from "../../format";
import type { Workspace, WorkspaceMember, WorkspaceRole } from "../../types";

const ROLE_HINT: Record<WorkspaceRole, string> = {
  owner: "可改工作区信息、管理成员、转让所有权",
  editor: "可增删自选、预警规则与回测",
  viewer: "只读，不能修改任何数据",
};

interface Props {
  current: Workspace;
  members: WorkspaceMember[] | undefined;
  membersLoading: boolean;
  membersError: boolean;
  onRetryMembers: () => void;
  canManage: boolean;
  adminViewing?: boolean;
  onFlash: (text: string) => void;
}

export default function MembersSection({
  current,
  members,
  membersLoading,
  membersError,
  onRetryMembers,
  canManage,
  adminViewing = false,
  onFlash,
}: Props) {
  const queryClient = useQueryClient();
  const user = useAuth((state) => state.user);
  const refresh = useWorkspace((state) => state.refresh);

  const [inviteOpen, setInviteOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [selected, setSelected] = useState<WorkspaceMember | null>(null);
  const [memberRole, setMemberRole] = useState<"editor" | "viewer">("viewer");
  const [removeTarget, setRemoveTarget] = useState<WorkspaceMember | null>(null);
  const [transferTarget, setTransferTarget] = useState<WorkspaceMember | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [search]);

  // 关闭邀请面板时清空选中，避免下次打开误加人。
  useEffect(() => {
    if (!inviteOpen) {
      setSelected(null);
      setSearch("");
    }
  }, [inviteOpen]);

  const candidates = useQuery({
    queryKey: ["workspace-candidates", current.id, debouncedSearch],
    queryFn: () => api.workspaceCandidates(current.id, debouncedSearch),
    enabled: canManage && inviteOpen,
    retry: false,
  });

  const addMember = useMutation({
    mutationFn: () => {
      if (!selected) throw new Error("请先选择要邀请的账号");
      return api.addWorkspaceMember(current.id, selected.username, memberRole);
    },
    onSuccess: (member) => {
      onFlash(`已加入 ${member.username}（${ROLE_LABEL[member.role]}）`);
      setSelected(null);
      setSearch("");
      setInviteOpen(false);
      queryClient.invalidateQueries({ queryKey: ["workspace-members", current.id] });
      queryClient.invalidateQueries({ queryKey: ["workspace-candidates", current.id] });
      queryClient.invalidateQueries({ queryKey: ["workspace-usage", current.id] });
      void refresh();
    },
    onError: (error) => onFlash(`添加失败：${(error as Error).message}`),
  });

  const changeRole = useMutation({
    mutationFn: ({ userId, role }: { userId: number; role: WorkspaceRole }) =>
      api.updateWorkspaceMember(current.id, userId, role),
    onSuccess: (member) => {
      onFlash(`已将 ${member.username || `user#${member.user_id}`} 设为${ROLE_LABEL[member.role]}`);
      queryClient.invalidateQueries({ queryKey: ["workspace-members", current.id] });
    },
    onError: (error) => onFlash(`改角色失败：${(error as Error).message}`),
  });

  const removeMember = useMutation({
    mutationFn: (userId: number) => api.removeWorkspaceMember(current.id, userId),
    onSuccess: () => {
      setRemoveTarget(null);
      onFlash("已移除成员");
      queryClient.invalidateQueries({ queryKey: ["workspace-members", current.id] });
      queryClient.invalidateQueries({ queryKey: ["workspace-usage", current.id] });
      void refresh();
    },
    onError: (error) => {
      setRemoveTarget(null);
      onFlash(`移除失败：${(error as Error).message}`);
    },
  });

  const transfer = useMutation({
    mutationFn: (userId: number) => api.transferWorkspace(current.id, userId),
    onSuccess: async () => {
      setTransferTarget(null);
      onFlash("已转让所有权，你现在的角色是编辑者");
      await refresh();
      queryClient.invalidateQueries({ queryKey: ["workspace-members", current.id] });
    },
    onError: (error) => {
      setTransferTarget(null);
      onFlash(`转让失败：${(error as Error).message}`);
    },
  });

  const rows = useMemo(() => members ?? [], [members]);
  const targetLabel = (row: WorkspaceMember | null) =>
    row ? row.username || row.nickname || `user#${row.user_id}` : "";

  return (
    <section id="members" className="scroll-mt-16">
      <div className="card">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold text-slate-100">成员 · {current.name}</h2>
            <p className="mt-1 text-xs text-slate-500">
              {adminViewing ? "管理员查看模式：成员列表只读，不能邀请/改角色/移除/转让。" : ROLE_HINT[current.role]}
            </p>
          </div>
          {canManage && (
            <button
              className="btn btn-primary"
              aria-expanded={inviteOpen}
              onClick={() => setInviteOpen((open) => !open)}
            >
              {inviteOpen ? "收起邀请" : "邀请成员"}
            </button>
          )}
        </div>

        {canManage && inviteOpen && (
          <div className="mb-3 rounded-md border border-slate-800 bg-slate-950/40 p-3">
            <div className="flex flex-wrap items-end gap-2">
              <div className="min-w-[16rem] flex-1">
                <label className="label" htmlFor="member-search">
                  ⌕ 用户名或昵称
                </label>
                <input
                  id="member-search"
                  className="input w-full"
                  placeholder="输入以搜索可邀请的账号"
                  value={search}
                  onChange={(event) => setSearch(event.target.value)}
                  autoComplete="off"
                />
              </div>
              <div>
                <label className="label" htmlFor="member-role">
                  角色
                </label>
                <select
                  id="member-role"
                  className="input"
                  value={memberRole}
                  onChange={(event) => setMemberRole(event.target.value as "editor" | "viewer")}
                >
                  <option value="viewer">查看者（只读）</option>
                  <option value="editor">编辑者（可改数据）</option>
                </select>
              </div>
              <button
                className="btn btn-primary"
                disabled={!selected || addMember.isPending}
                onClick={() => addMember.mutate()}
              >
                {addMember.isPending ? "添加中…" : "添加成员"}
              </button>
              <button className="btn" type="button" onClick={() => setInviteOpen(false)}>
                取消
              </button>
            </div>

            <div className="mt-2">
              {candidates.isLoading && <p className="text-xs text-slate-500">候选账号加载中…</p>}
              {candidates.isError && (
                <p className="text-xs text-amber-300">
                  候选账号加载失败{" "}
                  <button className="underline" type="button" onClick={() => void candidates.refetch()}>
                    重试
                  </button>
                </p>
              )}
              {candidates.data && candidates.data.length === 0 && (
                <p className="text-xs text-slate-500">
                  {debouncedSearch ? "没有匹配的账号。" : "没有可添加的账号（已排除现有成员与禁用账号）。"}
                </p>
              )}
              {candidates.data && candidates.data.length > 0 && (
                <fieldset className="max-h-44 overflow-y-auto rounded-md border border-slate-800/70 p-2">
                  <legend className="px-1 text-[11px] text-slate-500">候选账号（单选）</legend>
                  <div className="flex flex-wrap gap-2">
                    {candidates.data.map((candidate) => (
                      <label
                        key={candidate.user_id}
                        className={`flex cursor-pointer items-center gap-2 rounded-md border px-2 py-1 text-xs ${
                          selected?.user_id === candidate.user_id
                            ? "border-sky-600 bg-sky-950/40 text-sky-200"
                            : "border-slate-800 text-slate-300 hover:border-slate-600"
                        }`}
                      >
                        <input
                          type="radio"
                          name="member-candidate"
                          className="accent-sky-500"
                          checked={selected?.user_id === candidate.user_id}
                          onChange={() => setSelected(candidate)}
                        />
                        <span className="font-medium">{candidate.username || `user#${candidate.user_id}`}</span>
                        {candidate.nickname && <span className="text-slate-500">{candidate.nickname}</span>}
                      </label>
                    ))}
                  </div>
                </fieldset>
              )}
              <p className="mt-1 text-[11px] text-slate-500">
                所有者只能通过「转让」产生；编辑者可写数据但不能管理成员。
              </p>
            </div>
          </div>
        )}

        <div className="table-wrap">
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-800">
                <th className="th">账号</th>
                <th className="th">昵称</th>
                <th className="th">角色</th>
                <th className="th">加入时间</th>
                <th className="th" />
              </tr>
            </thead>
            <tbody>
              {membersLoading && (
                <tr>
                  <td className="td text-slate-500" colSpan={5}>
                    加载中…
                  </td>
                </tr>
              )}
              {!membersLoading && membersError && (
                <tr>
                  <td className="td text-xs text-amber-300" colSpan={5}>
                    成员加载失败{" "}
                    <button className="underline" type="button" onClick={onRetryMembers}>
                      重试
                    </button>
                  </td>
                </tr>
              )}
              {!membersLoading && !membersError && rows.length === 0 && (
                <tr>
                  <td className="td text-slate-500" colSpan={5}>
                    还没有成员数据。
                  </td>
                </tr>
              )}
              {!membersLoading &&
                !membersError &&
                rows.map((row) => {
                  const isSelf = row.user_id === user?.id;
                  const editable = canManage && !isSelf;
                  return (
                    <tr key={row.user_id} className="border-b border-slate-800/50 last:border-0">
                      <td className="td font-medium text-slate-100">
                        {row.username || `user#${row.user_id}`}
                      </td>
                      <td className="td text-slate-400">{row.nickname || "—"}</td>
                      <td className="td">
                        {editable ? (
                          <select
                            className="input py-1 text-xs"
                            aria-label={`调整 ${row.username || `user#${row.user_id}`} 的角色`}
                            value={row.role}
                            disabled={changeRole.isPending}
                            onChange={(event) =>
                              changeRole.mutate({
                                userId: row.user_id,
                                role: event.target.value as WorkspaceRole,
                              })
                            }
                          >
                            {row.role === "owner" && <option value="owner">所有者</option>}
                            <option value="editor">编辑者</option>
                            <option value="viewer">查看者</option>
                          </select>
                        ) : (
                          <span className="text-slate-300">{ROLE_LABEL[row.role] ?? row.role}</span>
                        )}
                      </td>
                      <td className="td text-xs text-slate-400">{formatDateTime(row.joined_at)}</td>
                      <td className="td text-right">
                        {isSelf && <span className="text-xs text-slate-500">我</span>}
                        {editable && (
                          <div className="flex justify-end gap-1">
                            {row.role !== "owner" && (
                              <button
                                className="btn px-2 py-1 text-xs"
                                title="把所有权转给此人（自己降为编辑者）"
                                disabled={transfer.isPending}
                                onClick={() => setTransferTarget(row)}
                              >
                                转让
                              </button>
                            )}
                            <button
                              className="btn px-2 py-1 text-xs"
                              disabled={removeMember.isPending}
                              onClick={() => setRemoveTarget(row)}
                            >
                              移除
                            </button>
                          </div>
                        )}
                      </td>
                    </tr>
                  );
                })}
            </tbody>
          </table>
        </div>

        {!canManage && (
          <p className="mt-3 text-xs text-slate-500">
            {adminViewing
              ? "管理员查看模式下成员列表只读；如需管理成员，请先退出查看。"
              : `只有所有者可以增删成员或调整角色。需要变更请让「${current.name}」的所有者操作。`}
          </p>
        )}
      </div>

      <ConfirmDialog
        open={removeTarget !== null}
        title={`移除成员「${targetLabel(removeTarget)}」`}
        description="该账号将立即失去本工作区的访问权限；其在本工作区创建的数据仍归工作区所有，不会随成员一起删除。"
        confirmLabel="移除成员"
        danger
        busy={removeMember.isPending}
        onCancel={() => setRemoveTarget(null)}
        onConfirm={() => removeTarget && removeMember.mutate(removeTarget.user_id)}
      />

      <ConfirmDialog
        open={transferTarget !== null}
        title={`转让所有权给「${targetLabel(transferTarget)}」`}
        description={
          <>
            确认后「{current.name}」的所有权归对方所有，你将降为<strong>编辑者</strong>：
            不能再管理成员、通知通道、预警策略，也不能删除工作区。此操作不可撤销（但可再次转让）。
          </>
        }
        confirmLabel="转让所有权"
        danger
        busy={transfer.isPending}
        onCancel={() => setTransferTarget(null)}
        onConfirm={() => transferTarget && transfer.mutate(transferTarget.user_id)}
      />
    </section>
  );
}
