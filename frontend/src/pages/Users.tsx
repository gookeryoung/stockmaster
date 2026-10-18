/**
 * 管理员用户管理页：服务端分页 + 防抖搜索 + 创建/重置密码弹层 + 审计筛选。
 * 设计依据 docs/UI_AUTH.md §4.5 / §8。
 *
 * 权限由路由（仅 admin）与服务端 require_admin 保证；本页只负责减少误操作：
 * 自己不能禁用/降级/删除，危险操作二次确认，初始密码只在成功视图显示一次。
 */

import { FormEvent, useEffect, useId, useMemo, useRef, useState } from "react";
import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api } from "../api";
import { useAuth } from "../auth";
import { formatDateTime } from "../format";
import type { AdminUser, UserRole, UserStatus } from "../types";

const PAGE_SIZE = 20;
const AUDIT_PAGE = 30;
const USERNAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$/;
const PASSWORD_RULE = "密码至少 8 位，需同时包含字母和数字";

const STATUS_LABEL: Record<UserStatus, string> = { active: "启用", disabled: "已禁用" };
const ROLE_NAME: Record<UserRole, string> = { admin: "管理员", user: "普通用户" };

const AUDIT_ACTIONS: Array<{ value: string; label: string }> = [
  { value: "", label: "全部操作" },
  { value: "login.success", label: "登录成功" },
  { value: "login.failed", label: "登录失败" },
  { value: "login.ticket", label: "票据登录" },
  { value: "logout", label: "退出登录" },
  { value: "password.change", label: "修改密码" },
  { value: "user.register", label: "自助注册" },
  { value: "user.create", label: "创建账号" },
  { value: "user.update", label: "更新账号" },
  { value: "user.password_reset", label: "重置密码" },
  { value: "user.delete", label: "删除账号" },
  { value: "user.bootstrap", label: "引导管理员" },
  { value: "wechat.register", label: "微信注册" },
  { value: "wechat.bind", label: "绑定微信" },
  { value: "wechat.unbind", label: "解绑微信" },
];

type Notice = { kind: "ok" | "err"; text: string };
type FieldErrors = { username?: string; email?: string; password?: string };

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function validateUsername(raw: string): string {
  const value = raw.trim();
  if (!value) return "请输入用户名";
  if (!USERNAME_PATTERN.test(value)) {
    return "用户名需 3-64 位，以字母或数字开头，仅含字母、数字、点、下划线和连字符";
  }
  return "";
}

function validateEmail(raw: string): string {
  const value = raw.trim();
  if (!value) return "";
  if (value.length > 255 || value.startsWith("@") || value.endsWith("@") || !/^[^\s@]+@[^\s@]+$/.test(value)) {
    return "邮箱格式不正确";
  }
  return "";
}

function validatePassword(value: string): string {
  if (!value) return "请输入密码";
  if (value.length < 8 || value.length > 128) return PASSWORD_RULE;
  if (value.trim() !== value) return "密码不能以空白字符开头或结尾";
  if (!/[A-Za-z]/.test(value) || !/\d/.test(value)) return PASSWORD_RULE;
  return "";
}

/** 生成至少 12 位的强密码：大小写字母 + 数字 + 符号，浏览器 crypto 优先。 */
function generateStrongPassword(length = 16): string {
  const groups = [
    "abcdefghijkmnpqrstuvwxyz",
    "ABCDEFGHJKLMNPQRSTUVWXYZ",
    "23456789",
    "!@#$%^&*",
  ];
  const all = groups.join("");
  const randomIndex = (max: number) => {
    if (typeof window !== "undefined" && window.crypto?.getRandomValues) {
      const buffer = new Uint32Array(1);
      window.crypto.getRandomValues(buffer);
      return buffer[0] % max;
    }
    return Math.floor(Math.random() * max);
  };
  const chars = groups.map((group) => group[randomIndex(group.length)]);
  while (chars.length < length) chars.push(all[randomIndex(all.length)]);
  for (let index = chars.length - 1; index > 0; index -= 1) {
    const swap = randomIndex(index + 1);
    [chars[index], chars[swap]] = [chars[swap], chars[index]];
  }
  return chars.join("");
}

async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // 非安全上下文会抛错，退回到 execCommand 兜底。
  }
  try {
    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.style.position = "fixed";
    textarea.style.opacity = "0";
    document.body.appendChild(textarea);
    textarea.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(textarea);
    return ok;
  } catch {
    return false;
  }
}

export default function Users() {
  const queryClient = useQueryClient();
  const me = useAuth((state) => state.user);
  const config = useAuth((state) => state.config);
  const localMode = Boolean(config && !config.auth_enabled);
  const workspaceEnabled = Boolean(config?.workspace_enabled);

  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [page, setPage] = useState(1);
  const [feedback, setFeedback] = useState<Notice | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null);
  const [auditAction, setAuditAction] = useState("");
  const [auditLimit, setAuditLimit] = useState(AUDIT_PAGE);

  // 搜索 300ms 防抖；关键词变化回到第 1 页。
  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => window.clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    setPage(1);
  }, [debouncedSearch]);

  const users = useQuery({
    queryKey: ["users", debouncedSearch, page],
    queryFn: () => api.users({ q: debouncedSearch, page, page_size: PAGE_SIZE }),
    placeholderData: keepPreviousData,
  });

  const logs = useQuery({
    queryKey: ["audit-logs", auditAction, auditLimit],
    queryFn: () => api.auditLogs(auditLimit, auditAction),
  });

  const total = users.data?.total ?? 0;
  const rows = users.data?.items ?? [];
  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const auditRows = logs.data ?? [];

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  const pageNumbers = useMemo(() => {
    const start = Math.max(1, Math.min(page - 2, totalPages - 4));
    const end = Math.min(totalPages, start + 4);
    return Array.from({ length: end - start + 1 }, (_, index) => start + index);
  }, [page, totalPages]);

  const refresh = () => {
    void queryClient.invalidateQueries({ queryKey: ["users"] });
    void queryClient.invalidateQueries({ queryKey: ["audit-logs"] });
    void queryClient.invalidateQueries({ queryKey: ["auth-config"] });
  };

  const notify = (kind: Notice["kind"], text: string) => setFeedback({ kind, text });

  const patchUser = useMutation({
    mutationFn: ({
      id,
      payload,
    }: {
      id: number;
      payload: Record<string, unknown>;
      successText: string;
    }) => api.updateUser(id, payload),
    onSuccess: (_data, variables) => {
      notify("ok", variables.successText);
      refresh();
    },
    onError: (error) => notify("err", errorText(error)),
  });

  const removeUser = useMutation({
    mutationFn: ({ id }: { id: number; username: string }) => api.deleteUser(id),
    onSuccess: (_data, variables) => {
      notify("ok", `已删除账号 ${variables.username}`);
      refresh();
    },
    onError: (error) => notify("err", errorText(error)),
  });

  const changeSearch = (value: string) => {
    setSearch(value);
    setFeedback(null);
  };

  const confirmRoleChange = (row: AdminUser, nextRole: UserRole): boolean => {
    if (row.role === nextRole) return true;
    const name = row.nickname || row.username;
    const message =
      nextRole === "admin"
        ? `确认将 ${name} 的角色改为${ROLE_NAME[nextRole]}？管理员可管理全部账号与工作区。`
        : `确认将 ${name} 的角色改为${ROLE_NAME[nextRole]}？` +
          (row.role === "admin" ? "请确认系统中至少还保留一名启用的管理员。" : "");
    if (!window.confirm(message)) return false;
    patchUser.mutate({
      id: row.id,
      payload: { role: nextRole },
      successText: `已${nextRole === "admin" ? "提升" : "降级"} ${row.username} 的角色`,
    });
    return true;
  };

  const toggleStatus = (row: AdminUser) => {
    const nextStatus: UserStatus = row.status === "active" ? "disabled" : "active";
    const name = row.nickname || row.username;
    const message =
      nextStatus === "disabled"
        ? `确认禁用账号 ${name}？禁用后该账号将立即退出登录且无法再登录，可随时恢复。`
        : `确认恢复账号 ${name}？恢复后该账号可以重新登录。`;
    if (!window.confirm(message)) return;
    patchUser.mutate({
      id: row.id,
      payload: { status: nextStatus },
      successText:
        nextStatus === "disabled" ? `已禁用 ${row.username}，其会话已失效` : `已启用 ${row.username}`,
    });
  };

  const removeAccount = (row: AdminUser) => {
    const name = row.nickname || row.username;
    if (!window.confirm(`确认删除账号 ${name}（${row.username}）？该操作不可撤销。`)) return;
    if (
      !window.confirm(
        "再次确认：删除后无法恢复，该账号的历史审计记录仍会保留。若对方是最后一名启用的管理员，服务端会拒绝删除。",
      )
    ) {
      return;
    }
    removeUser.mutate({ id: row.id, username: row.username });
  };

  return (
    <div className="space-y-4">
      <div className="card space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="text-sm font-medium text-slate-300">用户管理</div>
          <span className="badge bg-slate-800 text-slate-400">共 {total} 个账号</span>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <div className="relative">
              <input
                className="input w-56 pr-7"
                placeholder="⌕ 搜索用户名 / 昵称 / 邮箱"
                value={search}
                onChange={(event) => changeSearch(event.target.value)}
                aria-label="搜索账号"
              />
              {search && (
                <button
                  type="button"
                  className="absolute inset-y-0 right-0 px-2 text-xs text-slate-500 hover:text-slate-300"
                  onClick={() => changeSearch("")}
                  aria-label="清除搜索"
                >
                  ×
                </button>
              )}
            </div>
            <button
              type="button"
              className="btn"
              onClick={() => void users.refetch()}
              disabled={users.isFetching}
            >
              {users.isFetching ? "刷新中…" : "刷新"}
            </button>
            <button type="button" className="btn btn-primary" onClick={() => setCreateOpen(true)}>
              + 新建账号
            </button>
          </div>
        </div>

        {localMode && (
          <div className="rounded-md border border-amber-900/60 bg-amber-950/40 px-3 py-2 text-xs leading-relaxed text-amber-300">
            当前未开启登录校验（SM_AUTH_ENABLED=false）：任何能访问本机的人都可以管理账号。
            对外暴露前请设置 SM_AUTH_ENABLED=true。
          </div>
        )}

        {feedback && (
          <div
            role={feedback.kind === "err" ? "alert" : "status"}
            aria-live="polite"
            className={`rounded-md border px-3 py-2 text-xs leading-relaxed ${
              feedback.kind === "ok"
                ? "border-emerald-900/60 bg-emerald-950/40 text-emerald-300"
                : "border-red-900/60 bg-red-950/40 text-red-300"
            }`}
          >
            {feedback.text}
          </div>
        )}

        <div className="table-wrap">
          <table className="w-full min-w-[880px]">
            <thead>
              <tr className="border-b border-slate-800">
                <th className="th">用户</th>
                <th className="th">角色</th>
                <th className="th">状态</th>
                <th className="th">登录方式</th>
                <th className="th">最近登录</th>
                <th className="th" title="参与的工作区数量（含自己是所有者的）">
                  工作区
                </th>
                <th className="th" title="仅统计该账号个人工作区的数据量，不含共享工作区">
                  个人数据量
                </th>
                <th className="th text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {users.isLoading && (
                <tr>
                  <td className="td" colSpan={8}>
                    <div className="space-y-2 py-2" aria-label="用户列表加载中">
                      {[0, 1, 2].map((index) => (
                        <div key={index} className="flex gap-3">
                          <div className="h-7 w-7 animate-pulse rounded-full bg-slate-800/70" />
                          <div className="h-4 flex-1 animate-pulse rounded bg-slate-800/60" />
                          <div className="h-4 w-24 animate-pulse rounded bg-slate-800/50" />
                        </div>
                      ))}
                    </div>
                  </td>
                </tr>
              )}

              {!users.isLoading && users.isError && (
                <tr>
                  <td className="td" colSpan={8}>
                    <div className="flex flex-wrap items-center gap-2 text-xs text-amber-300">
                      用户列表加载失败：{errorText(users.error)}
                      <button
                        type="button"
                        className="btn min-h-[32px] px-2 py-1 text-xs"
                        onClick={() => void users.refetch()}
                      >
                        重试
                      </button>
                    </div>
                  </td>
                </tr>
              )}

              {!users.isLoading && !users.isError && rows.length === 0 && (
                <tr>
                  <td className="td text-slate-500" colSpan={8}>
                    {debouncedSearch ? (
                      <span>
                        没有匹配「{debouncedSearch}」的账号。
                        <button
                          type="button"
                          className="ml-2 text-xs text-sky-400 hover:underline"
                          onClick={() => changeSearch("")}
                        >
                          清除搜索
                        </button>
                      </span>
                    ) : (
                      "还没有任何账号。"
                    )}
                  </td>
                </tr>
              )}

              {rows.map((row) => {
                const isSelf = me?.id === row.id;
                const loginMethods =
                  [
                    row.has_password ? "密码" : null,
                    ...row.identities.map((identity) =>
                      identity.provider === "wechat" ? "微信" : identity.provider,
                    ),
                  ]
                    .filter(Boolean)
                    .join(" / ") || "—";
                const guardTitle = isSelf
                  ? "不能修改自己的角色或状态，请让其他管理员操作"
                  : undefined;
                return (
                  <tr key={row.id} className="border-b border-slate-800/60 last:border-0">
                    <td className="td">
                      <div className="flex items-center gap-2">
                        <Avatar user={row} />
                        <div className="min-w-0">
                          <div className="truncate text-slate-100">
                            {row.nickname || row.username}
                            {isSelf && <span className="ml-1 text-[10px] text-sky-400">我</span>}
                          </div>
                          <div className="truncate text-[11px] text-slate-500">
                            {row.username}
                            {row.email ? ` · ${row.email}` : ""}
                          </div>
                        </div>
                      </div>
                    </td>
                    <td className="td">
                      <select
                        className="input py-1"
                        value={row.role}
                        disabled={isSelf || patchUser.isPending}
                        title={guardTitle}
                        aria-label={`${row.username} 的角色`}
                        onChange={(event) => {
                          const nextRole = event.target.value as UserRole;
                          if (!confirmRoleChange(row, nextRole)) {
                            // 取消确认时回滚受控 select 的显示值。
                            event.target.value = row.role;
                          }
                        }}
                      >
                        <option value="user">普通用户</option>
                        <option value="admin">管理员</option>
                      </select>
                    </td>
                    <td className="td">
                      <button
                        type="button"
                        className={`badge ${
                          row.status === "active"
                            ? "bg-emerald-900/50 text-emerald-300"
                            : "bg-slate-800 text-slate-400"
                        }`}
                        disabled={isSelf || patchUser.isPending}
                        title={isSelf ? guardTitle : "点击切换启用状态"}
                        onClick={() => toggleStatus(row)}
                      >
                        {STATUS_LABEL[row.status]}
                      </button>
                    </td>
                    <td className="td text-xs text-slate-400">{loginMethods}</td>
                    <td className="td text-xs text-slate-400">{formatDateTime(row.last_login_at)}</td>
                    <td
                      className="td text-xs text-slate-400"
                      title={
                        workspaceEnabled
                          ? `其中 ${row.owned_workspace_count} 个为所有者`
                          : "未启用数据隔离，无工作区概念"
                      }
                    >
                      {workspaceEnabled ? `${row.workspace_count} 个` : "—"}
                    </td>
                    <td
                      className="td text-xs text-slate-400"
                      title={workspaceEnabled ? "仅统计个人工作区的数据，不含共享工作区" : "未启用数据隔离"}
                    >
                      {workspaceEnabled ? `${row.personal_data_total} 条` : "—"}
                    </td>
                    <td className="td">
                      <div className="flex flex-wrap justify-end gap-1">
                        <button
                          type="button"
                          className="btn min-h-[32px] px-2 py-1 text-xs"
                          disabled={isSelf}
                          title={isSelf ? "请到「账号设置」修改自己的密码" : "生成或设置新密码"}
                          onClick={() => setResetTarget(row)}
                        >
                          重置密码
                        </button>
                        <button
                          type="button"
                          className="btn min-h-[32px] px-2 py-1 text-xs hover:border-red-700 hover:bg-red-950/60"
                          disabled={isSelf || removeUser.isPending}
                          title={isSelf ? "不能删除自己的账号" : "删除账号（不可撤销）"}
                          onClick={() => removeAccount(row)}
                        >
                          {removeUser.isPending && removeUser.variables?.id === row.id
                            ? "删除中…"
                            : "删除"}
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-slate-400">
          <span>
            共 {total} 个账号 · 第 {page}/{totalPages} 页
            {users.isFetching && !users.isLoading ? " · 更新中…" : ""}
          </span>
          <div className="flex flex-wrap items-center gap-1" role="navigation" aria-label="分页">
            <button
              type="button"
              className="btn min-h-[32px] px-2 py-1 text-xs"
              disabled={page <= 1}
              onClick={() => setPage((value) => Math.max(1, value - 1))}
            >
              ‹ 上一页
            </button>
            {pageNumbers.map((number) => (
              <button
                key={number}
                type="button"
                className={`btn min-h-[32px] px-2 py-1 text-xs ${number === page ? "btn-primary" : ""}`}
                aria-current={number === page ? "page" : undefined}
                onClick={() => setPage(number)}
              >
                {number}
              </button>
            ))}
            <button
              type="button"
              className="btn min-h-[32px] px-2 py-1 text-xs"
              disabled={page >= totalPages}
              onClick={() => setPage((value) => Math.min(totalPages, value + 1))}
            >
              下一页 ›
            </button>
          </div>
        </div>
      </div>

      {/* 审计 */}
      <div className="card space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <div className="text-sm font-medium text-slate-300">最近操作</div>
          <span className="text-[11px] text-slate-500">登录、绑定、改密与账号变更留痕</span>
          <select
            className="input ml-auto py-1 text-xs"
            value={auditAction}
            onChange={(event) => {
              setAuditAction(event.target.value);
              setAuditLimit(AUDIT_PAGE);
            }}
            aria-label="按操作类型筛选审计日志"
          >
            {AUDIT_ACTIONS.map((item) => (
              <option key={item.value || "all"} value={item.value}>
                {item.label}
                {item.value ? `（${item.value}）` : ""}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="btn min-h-[32px] px-2 py-1 text-xs"
            onClick={() => void logs.refetch()}
            disabled={logs.isFetching}
          >
            {logs.isFetching ? "加载中…" : "刷新"}
          </button>
        </div>

        {logs.isLoading && <p className="text-xs text-slate-500">审计日志加载中…</p>}

        {logs.isError && (
          <div className="flex flex-wrap items-center gap-2 text-xs text-amber-300">
            审计日志加载失败：{errorText(logs.error)}
            <button type="button" className="btn min-h-[32px] px-2 py-1 text-xs" onClick={() => void logs.refetch()}>
              重试
            </button>
          </div>
        )}

        {!logs.isLoading && !logs.isError && auditRows.length === 0 && (
          <p className="text-xs text-slate-500">
            {auditAction ? "该操作类型暂无记录。" : "暂无记录。"}
          </p>
        )}

        {!logs.isError && auditRows.length > 0 && (
          <div className="max-h-72 space-y-1 overflow-auto text-xs">
            {auditRows.map((log) => (
              <div
                key={log.id}
                className="flex flex-wrap items-center gap-x-2 border-b border-slate-800/60 py-1"
              >
                <span className="w-32 shrink-0 text-slate-500">{formatDateTime(log.created_at)}</span>
                <span className="badge bg-slate-800 text-slate-300">{log.action}</span>
                <span className="text-slate-300">{log.username || "—"}</span>
                {log.detail && <span className="text-slate-500">{log.detail}</span>}
                {log.ip && <span className="text-slate-600">{log.ip}</span>}
              </div>
            ))}
          </div>
        )}

        {!logs.isError && auditRows.length >= auditLimit && (
          <div className="flex justify-center">
            <button
              type="button"
              className="btn min-h-[32px] px-2 py-1 text-xs"
              onClick={() => setAuditLimit((value) => value + AUDIT_PAGE)}
              disabled={logs.isFetching}
            >
              {logs.isFetching ? "加载中…" : "加载更多"}
            </button>
          </div>
        )}
      </div>

      {createOpen && (
        <CreateUserDialog
          onClose={() => setCreateOpen(false)}
          onDone={(message) => {
            setCreateOpen(false);
            notify("ok", message);
            refresh();
          }}
        />
      )}

      {resetTarget && (
        <ResetPasswordDialog
          user={resetTarget}
          onClose={() => setResetTarget(null)}
          onDone={(message) => {
            setResetTarget(null);
            notify("ok", message);
            refresh();
          }}
        />
      )}
    </div>
  );
}

function Avatar({ user }: { user: AdminUser }) {
  if (user.avatar_url) {
    return <img src={user.avatar_url} alt="" className="h-7 w-7 rounded-full object-cover" />;
  }
  return (
    <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-slate-800 text-xs text-slate-300">
      {(user.nickname || user.username).slice(0, 1).toUpperCase()}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 通用弹层
// ---------------------------------------------------------------------------

function Modal({
  title,
  onClose,
  busy = false,
  children,
}: {
  title: string;
  onClose: () => void;
  /** 提交中禁止关闭，避免自动生成的密码还没展示就随弹层丢失。 */
  busy?: boolean;
  children: ReactNode;
}) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement | null>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const busyRef = useRef(busy);
  busyRef.current = busy;
  const requestClose = () => {
    if (!busyRef.current) closeRef.current();
  };

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const timer = window.setTimeout(() => panelRef.current?.focus(), 0);
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        requestClose();
        return;
      }
      if (event.key !== "Tab" || !panelRef.current) return;
      const focusables = Array.from(
        panelRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => element.offsetParent !== null);
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panelRef.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("keydown", handleKeyDown);
      previous?.focus?.();
    };
  }, []);

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 px-4 py-6"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) requestClose();
      }}
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="w-full max-w-[460px] rounded-lg border border-slate-800 bg-slate-900 p-4 shadow-2xl shadow-black/40 outline-none"
      >
        <div className="flex items-start gap-3">
          <div id={titleId} className="min-w-0 flex-1 text-sm font-medium text-slate-200">
            {title}
          </div>
          <button
            type="button"
            className="btn min-h-[32px] px-2 py-1 text-xs"
            onClick={requestClose}
            disabled={busy}
          >
            关闭
          </button>
        </div>
        <div className="mt-3">{children}</div>
      </div>
    </div>
  );
}

function CopyField({ value, label }: { value: string; label: string }) {
  const id = useId();
  const [copied, setCopied] = useState(false);
  return (
    <div>
      <label className="label" htmlFor={id}>
        {label}
      </label>
      <div className="flex gap-2">
        <input
          id={id}
          className="input w-full font-mono"
          value={value}
          readOnly
          onFocus={(event) => event.currentTarget.select()}
        />
        <button
          type="button"
          className="btn min-h-[32px] px-2 py-1 text-xs"
          onClick={() => {
            void copyToClipboard(value).then((ok) => {
              if (!ok) return;
              setCopied(true);
              window.setTimeout(() => setCopied(false), 2000);
            });
          }}
        >
          {copied ? "已复制" : "复制"}
        </button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 创建账号
// ---------------------------------------------------------------------------

function CreateUserDialog({
  onClose,
  onDone,
}: {
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const queryClient = useQueryClient();
  const [username, setUsername] = useState("");
  const [nickname, setNickname] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<UserRole>("user");
  const [mode, setMode] = useState<"auto" | "manual">("auto");
  const [manualPassword, setManualPassword] = useState("");
  const [showManual, setShowManual] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState("");
  const [created, setCreated] = useState<{ username: string; password: string } | null>(null);
  const doneRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (created) requestAnimationFrame(() => doneRef.current?.focus());
  }, [created]);

  const create = useMutation({
    mutationFn: (password: string) =>
      api.createUser({
        username: username.trim(),
        password,
        nickname: nickname.trim(),
        email: email.trim() || undefined,
        role,
      }),
    onSuccess: (user, password) => {
      setCreated({ username: user.username, password });
      void queryClient.invalidateQueries({ queryKey: ["users"] });
      void queryClient.invalidateQueries({ queryKey: ["audit-logs"] });
      void queryClient.invalidateQueries({ queryKey: ["auth-config"] });
    },
    onError: (error) => {
      const message = errorText(error);
      if (message.includes("用户名")) setErrors({ username: message });
      else if (message.includes("邮箱")) setErrors({ email: message });
      else if (message.includes("密码")) setErrors({ password: message });
      else setFormError(message);
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setFormError("");
    const nextErrors: FieldErrors = {};
    const usernameError = validateUsername(username);
    if (usernameError) nextErrors.username = usernameError;
    const emailError = validateEmail(email);
    if (emailError) nextErrors.email = emailError;
    if (mode === "manual") {
      const passwordError = validatePassword(manualPassword);
      if (passwordError) nextErrors.password = passwordError;
    }
    const first = (["username", "email", "password"] as const).find((field) => nextErrors[field]);
    if (first) {
      setErrors(nextErrors);
      requestAnimationFrame(() => document.getElementById(`create-${first}`)?.focus());
      return;
    }
    setErrors({});
    create.mutate(mode === "auto" ? generateStrongPassword() : manualPassword);
  };

  if (created) {
    return (
      <Modal title="账号已创建" onClose={() => onDone(`已创建账号 ${created.username}`)}>
        <div className="space-y-3">
          <p className="text-xs leading-relaxed text-slate-400">
            账号 <span className="text-slate-200">{created.username}</span> 已创建。
            初始密码只在这里显示一次，请立即复制并转交本人，提醒其首次登录后修改密码。
          </p>
          <CopyField label="初始密码（仅显示一次）" value={created.password} />
          <div className="flex justify-end">
            <button
              ref={doneRef}
              type="button"
              className="btn btn-primary"
              onClick={() => onDone(`已创建账号 ${created.username}`)}
            >
              完成
            </button>
          </div>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title="新建账号" onClose={onClose} busy={create.isPending}>
      <form className="space-y-3" onSubmit={submit} noValidate>
        <div>
          <label className="label" htmlFor="create-username">
            用户名 *
          </label>
          <input
            id="create-username"
            className={`input w-full ${errors.username ? "border-red-800" : ""}`}
            value={username}
            onChange={(event) => {
              setUsername(event.target.value);
              setErrors((prev) => ({ ...prev, username: undefined }));
            }}
            maxLength={64}
            autoComplete="off"
            aria-invalid={Boolean(errors.username)}
            aria-describedby={errors.username ? "create-username-error" : undefined}
            placeholder="3-64 位，字母或数字开头"
          />
          {errors.username && (
            <p id="create-username-error" role="alert" className="mt-1 text-[11px] text-red-300">
              {errors.username}
            </p>
          )}
        </div>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <div>
            <label className="label" htmlFor="create-nickname">
              昵称（可选）
            </label>
            <input
              id="create-nickname"
              className="input w-full"
              value={nickname}
              onChange={(event) => setNickname(event.target.value)}
              maxLength={64}
              placeholder="留空则使用用户名"
            />
          </div>
          <div>
            <label className="label" htmlFor="create-email">
              邮箱（可选）
            </label>
            <input
              id="create-email"
              className={`input w-full ${errors.email ? "border-red-800" : ""}`}
              type="email"
              value={email}
              onChange={(event) => {
                setEmail(event.target.value);
                setErrors((prev) => ({ ...prev, email: undefined }));
              }}
              maxLength={255}
              aria-invalid={Boolean(errors.email)}
              aria-describedby={errors.email ? "create-email-error" : undefined}
            />
            {errors.email && (
              <p id="create-email-error" role="alert" className="mt-1 text-[11px] text-red-300">
                {errors.email}
              </p>
            )}
          </div>
        </div>
        <div>
          <label className="label" htmlFor="create-role">
            角色
          </label>
          <select
            id="create-role"
            className="input w-full"
            value={role}
            onChange={(event) => setRole(event.target.value as UserRole)}
          >
            <option value="user">普通用户</option>
            <option value="admin">管理员</option>
          </select>
        </div>

        <fieldset>
          <legend className="label">初始密码</legend>
          <div className="space-y-2">
            <label className="flex items-center gap-2 text-sm text-slate-200" htmlFor="create-password-auto">
              <input
                id="create-password-auto"
                type="radio"
                name="create-password-mode"
                checked={mode === "auto"}
                onChange={() => setMode("auto")}
              />
              自动生成强密码（创建成功后仅显示一次）
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-200" htmlFor="create-password-manual">
              <input
                id="create-password-manual"
                type="radio"
                name="create-password-mode"
                checked={mode === "manual"}
                onChange={() => setMode("manual")}
              />
              手动设置
            </label>
          </div>
        </fieldset>

        {mode === "manual" && (
          <div>
            <label className="label" htmlFor="create-password">
              初始密码 *
            </label>
            <div className="relative">
              <input
                id="create-password"
                className={`input w-full pr-14 ${errors.password ? "border-red-800" : ""}`}
                type={showManual ? "text" : "password"}
                value={manualPassword}
                onChange={(event) => {
                  setManualPassword(event.target.value);
                  setErrors((prev) => ({ ...prev, password: undefined }));
                }}
                autoComplete="new-password"
                aria-invalid={Boolean(errors.password)}
                aria-describedby={errors.password ? "create-password-error" : "create-password-rule"}
                placeholder="至少 8 位，需同时包含字母和数字"
              />
              <button
                type="button"
                onClick={() => setShowManual((value) => !value)}
                aria-label={showManual ? "隐藏初始密码" : "显示初始密码"}
                aria-pressed={showManual}
                className="absolute inset-y-0 right-0 px-3 text-xs text-slate-400 transition hover:text-slate-200"
              >
                {showManual ? "隐藏" : "显示"}
              </button>
            </div>
            <p id="create-password-rule" className="mt-1 text-[11px] text-slate-500">
              至少 8 位，需同时包含字母和数字；不能以空白字符开头或结尾。
            </p>
            {errors.password && (
              <p id="create-password-error" role="alert" className="mt-1 text-[11px] text-red-300">
                {errors.password}
              </p>
            )}
          </div>
        )}

        {formError && (
          <p role="alert" className="rounded-md border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
            {formError}
          </p>
        )}

        <div className="flex justify-end gap-2">
          <button type="button" className="btn" onClick={onClose} disabled={create.isPending}>
            取消
          </button>
          <button type="submit" className="btn btn-primary" disabled={create.isPending}>
            {create.isPending ? "创建中…" : "创建账号"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

// ---------------------------------------------------------------------------
// 重置密码
// ---------------------------------------------------------------------------

function ResetPasswordDialog({
  user,
  onClose,
  onDone,
}: {
  user: AdminUser;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const queryClient = useQueryClient();
  const [mode, setMode] = useState<"auto" | "manual">("auto");
  const [manualPassword, setManualPassword] = useState("");
  const [showManual, setShowManual] = useState(false);
  const [error, setError] = useState("");
  const [formError, setFormError] = useState("");
  const [done, setDone] = useState<string | null>(null);
  const doneRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (done) requestAnimationFrame(() => doneRef.current?.focus());
  }, [done]);

  const reset = useMutation({
    mutationFn: (password: string) => api.resetUserPassword(user.id, password),
    onSuccess: (_data, password) => {
      setDone(password);
      void queryClient.invalidateQueries({ queryKey: ["users"] });
      void queryClient.invalidateQueries({ queryKey: ["audit-logs"] });
    },
    onError: (err) => setFormError(errorText(err)),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    setFormError("");
    if (mode === "manual") {
      const passwordError = validatePassword(manualPassword);
      if (passwordError) {
        setError(passwordError);
        requestAnimationFrame(() => document.getElementById("reset-password")?.focus());
        return;
      }
    }
    setError("");
    reset.mutate(mode === "auto" ? generateStrongPassword() : manualPassword);
  };

  const message = `已重置 ${user.username} 的密码，该账号需重新登录`;

  if (done) {
    return (
      <Modal title="密码已重置" onClose={() => onDone(message)}>
        <div className="space-y-3">
          <p className="text-xs leading-relaxed text-slate-400">
            已重置 <span className="text-slate-200">{user.username}</span> 的密码。
            该账号所有已签发的登录状态已失效，需用新密码重新登录。
          </p>
          <CopyField label="新密码（仅显示一次）" value={done} />
          <div className="flex justify-end">
            <button
              ref={doneRef}
              type="button"
              className="btn btn-primary"
              onClick={() => onDone(message)}
            >
              完成
            </button>
          </div>
        </div>
      </Modal>
    );
  }

  return (
    <Modal title={`重置密码 · ${user.username}`} onClose={onClose} busy={reset.isPending}>
      <form className="space-y-3" onSubmit={submit} noValidate>
        <p className="text-[11px] leading-relaxed text-slate-500">
          重置后该账号所有已签发的登录状态立即失效。新密码只在成功后显示一次。
        </p>

        <fieldset>
          <legend className="label">新密码方式</legend>
          <div className="space-y-2">
            <label className="flex items-center gap-2 text-sm text-slate-200" htmlFor="reset-password-auto">
              <input
                id="reset-password-auto"
                type="radio"
                name="reset-password-mode"
                checked={mode === "auto"}
                onChange={() => setMode("auto")}
              />
              自动生成强密码
            </label>
            <label className="flex items-center gap-2 text-sm text-slate-200" htmlFor="reset-password-manual">
              <input
                id="reset-password-manual"
                type="radio"
                name="reset-password-mode"
                checked={mode === "manual"}
                onChange={() => setMode("manual")}
              />
              手动设置
            </label>
          </div>
        </fieldset>

        {mode === "manual" && (
          <div>
            <label className="label" htmlFor="reset-password">
              新密码 *
            </label>
            <div className="relative">
              <input
                id="reset-password"
                className={`input w-full pr-14 ${error ? "border-red-800" : ""}`}
                type={showManual ? "text" : "password"}
                value={manualPassword}
                onChange={(event) => {
                  setManualPassword(event.target.value);
                  setError("");
                }}
                autoComplete="new-password"
                aria-invalid={Boolean(error)}
                aria-describedby={error ? "reset-password-error" : "reset-password-rule"}
                placeholder="至少 8 位，需同时包含字母和数字"
              />
              <button
                type="button"
                onClick={() => setShowManual((value) => !value)}
                aria-label={showManual ? "隐藏新密码" : "显示新密码"}
                aria-pressed={showManual}
                className="absolute inset-y-0 right-0 px-3 text-xs text-slate-400 transition hover:text-slate-200"
              >
                {showManual ? "隐藏" : "显示"}
              </button>
            </div>
            <p id="reset-password-rule" className="mt-1 text-[11px] text-slate-500">
              至少 8 位，需同时包含字母和数字；不能以空白字符开头或结尾。
            </p>
            {error && (
              <p id="reset-password-error" role="alert" className="mt-1 text-[11px] text-red-300">
                {error}
              </p>
            )}
          </div>
        )}

        {formError && (
          <p role="alert" className="rounded-md border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
            {formError}
          </p>
        )}

        <div className="flex justify-end gap-2">
          <button type="button" className="btn" onClick={onClose} disabled={reset.isPending}>
            取消
          </button>
          <button type="submit" className="btn btn-primary" disabled={reset.isPending}>
            {reset.isPending ? "重置中…" : "确认重置"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
