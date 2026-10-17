import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import type { AuthUser, UserRole, UserStatus } from "../types";
import { formatDateTime } from "../format";

const STATUS_LABEL: Record<UserStatus, string> = { active: "启用", disabled: "已禁用" };

export default function Users() {
  const queryClient = useQueryClient();
  const me = useAuth((state) => state.user);

  const [q, setQ] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [feedback, setFeedback] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [resetTarget, setResetTarget] = useState<AuthUser | null>(null);

  const users = useQuery({
    queryKey: ["users", q],
    queryFn: () => api.users({ q, page_size: 100 }),
  });
  const logs = useQuery({ queryKey: ["audit-logs"], queryFn: () => api.auditLogs(30) });

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ["users"] });
    queryClient.invalidateQueries({ queryKey: ["audit-logs"] });
    queryClient.invalidateQueries({ queryKey: ["auth-config"] });
  };

  const notify = (kind: "ok" | "err", text: string) => setFeedback({ kind, text });

  const patchUser = useMutation({
    mutationFn: ({ id, payload }: { id: number; payload: Record<string, unknown> }) =>
      api.updateUser(id, payload),
    onSuccess: () => {
      notify("ok", "已更新");
      refresh();
    },
    onError: (error) => notify("err", (error as Error).message),
  });

  const removeUser = useMutation({
    mutationFn: (id: number) => api.deleteUser(id),
    onSuccess: () => {
      notify("ok", "已删除");
      refresh();
    },
    onError: (error) => notify("err", (error as Error).message),
  });

  const rows = users.data?.items ?? [];

  return (
    <div className="space-y-4">
      <div className="card space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="text-sm font-medium text-slate-300">用户管理</div>
          <span className="badge bg-slate-800 text-slate-400">共 {users.data?.total ?? 0} 个账号</span>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <input
              className="input w-44"
              placeholder="搜索用户名 / 昵称 / 邮箱"
              value={q}
              onChange={(e) => setQ(e.target.value)}
            />
            <button className="btn" onClick={() => users.refetch()} disabled={users.isFetching}>
              刷新
            </button>
            <button className="btn btn-primary" onClick={() => setShowCreate((v) => !v)}>
              {showCreate ? "收起" : "新建账号"}
            </button>
          </div>
        </div>

        {feedback && (
          <div
            className={`rounded-md border px-3 py-2 text-xs ${
              feedback.kind === "ok"
                ? "border-emerald-900/60 bg-emerald-950/40 text-emerald-300"
                : "border-red-900/60 bg-red-950/40 text-red-300"
            }`}
          >
            {feedback.text}
          </div>
        )}

        {showCreate && (
          <CreateForm
            onCreated={(name) => {
              setShowCreate(false);
              notify("ok", `已创建账号 ${name}`);
              refresh();
            }}
            onError={(message) => notify("err", message)}
          />
        )}

        <div className="table-wrap">
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-800">
                <th className="th">用户</th>
                <th className="th">角色</th>
                <th className="th">状态</th>
                <th className="th">登录方式</th>
                <th className="th">最近登录</th>
                <th className="th">工作区</th>
                <th className="th">个人数据量</th>
                <th className="th">操作</th>
              </tr>
            </thead>
            <tbody>
              {users.isLoading && (
                <tr>
                  <td className="td text-slate-500" colSpan={8}>
                    加载中…
                  </td>
                </tr>
              )}
              {!users.isLoading && rows.length === 0 && (
                <tr>
                  <td className="td text-slate-500" colSpan={8}>
                    没有匹配的账号
                  </td>
                </tr>
              )}
              {rows.map((row) => {
                const isSelf = me?.id === row.id;
                return (
                  <tr key={row.id} className="border-b border-slate-800/60 last:border-0">
                    <td className="td">
                      <div className="flex items-center gap-2">
                        <Avatar user={row} />
                        <div>
                          <div className="text-slate-100">
                            {row.nickname || row.username}
                            {isSelf && <span className="ml-1 text-[10px] text-sky-400">我</span>}
                          </div>
                          <div className="text-[11px] text-slate-500">
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
                        onChange={(e) =>
                          patchUser.mutate({ id: row.id, payload: { role: e.target.value } })
                        }
                      >
                        <option value="admin">管理员</option>
                        <option value="user">普通用户</option>
                      </select>
                    </td>
                    <td className="td">
                      <button
                        className={`badge ${
                          row.status === "active"
                            ? "bg-emerald-900/50 text-emerald-300"
                            : "bg-slate-800 text-slate-400"
                        }`}
                        disabled={isSelf || patchUser.isPending}
                        onClick={() =>
                          patchUser.mutate({
                            id: row.id,
                            payload: { status: row.status === "active" ? "disabled" : "active" },
                          })
                        }
                        title={isSelf ? "不能禁用自己" : "点击切换启用状态"}
                      >
                        {STATUS_LABEL[row.status]}
                      </button>
                    </td>
                    <td className="td text-xs text-slate-400">
                      {[
                        row.has_password ? "密码" : null,
                        ...row.identities.map((i) => (i.provider === "wechat" ? "微信" : i.provider)),
                      ]
                        .filter(Boolean)
                        .join(" / ") || "—"}
                    </td>
                    <td className="td text-xs text-slate-400">{formatDateTime(row.last_login_at)}</td>
                    <td
                      className="td text-xs text-slate-400"
                      title={`其中 ${row.owned_workspace_count} 个为所有者`}
                    >
                      {row.workspace_count} 个
                    </td>
                    <td
                      className="td text-xs text-slate-400"
                      title="仅统计个人工作区的数据，不含共享工作区"
                    >
                      {row.personal_data_total} 条
                    </td>
                    <td className="td">
                      <div className="flex gap-1">
                        <button className="btn px-2 py-1 text-xs" onClick={() => setResetTarget(row)}>
                          重置密码
                        </button>
                        <button
                          className="btn px-2 py-1 text-xs hover:border-red-700 hover:bg-red-950/60"
                          disabled={isSelf || removeUser.isPending}
                          onClick={() => {
                            if (window.confirm(`确认删除账号 ${row.username}？该操作不可撤销。`)) {
                              removeUser.mutate(row.id);
                            }
                          }}
                        >
                          删除
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card space-y-2">
        <div className="flex items-center gap-2">
          <div className="text-sm font-medium text-slate-300">最近操作</div>
          <span className="text-[11px] text-slate-500">登录、绑定、改密与账号变更留痕</span>
        </div>
        <div className="max-h-64 space-y-1 overflow-auto text-xs">
          {(logs.data ?? []).length === 0 && <div className="text-slate-500">暂无记录</div>}
          {(logs.data ?? []).map((log) => (
            <div key={log.id} className="flex flex-wrap items-center gap-x-2 border-b border-slate-800/60 py-1">
              <span className="w-32 shrink-0 text-slate-500">{formatDateTime(log.created_at)}</span>
              <span className="badge bg-slate-800 text-slate-300">{log.action}</span>
              <span className="text-slate-300">{log.username || "—"}</span>
              {log.detail && <span className="text-slate-500">{log.detail}</span>}
              {log.ip && <span className="text-slate-600">{log.ip}</span>}
            </div>
          ))}
        </div>
      </div>

      {resetTarget && (
        <PasswordDialog
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

function Avatar({ user }: { user: AuthUser }) {
  if (user.avatar_url) {
    return <img src={user.avatar_url} alt="" className="h-7 w-7 rounded-full object-cover" />;
  }
  return (
    <div className="flex h-7 w-7 items-center justify-center rounded-full bg-slate-800 text-xs text-slate-300">
      {(user.nickname || user.username).slice(0, 1).toUpperCase()}
    </div>
  );
}

function CreateForm({ onCreated, onError }: { onCreated: (name: string) => void; onError: (msg: string) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [nickname, setNickname] = useState("");
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<UserRole>("user");

  const create = useMutation({
    mutationFn: () =>
      api.createUser({
        username: username.trim(),
        password,
        nickname: nickname.trim(),
        email: email.trim() || undefined,
        role,
      }),
    onSuccess: (user) => {
      setUsername("");
      setPassword("");
      setNickname("");
      setEmail("");
      setRole("user");
      onCreated(user.username);
    },
    onError: (error) => onError((error as Error).message),
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    create.mutate();
  };

  return (
    <form onSubmit={submit} className="grid grid-cols-1 gap-2 rounded-md border border-slate-800 bg-slate-950/50 p-3 md:grid-cols-2 xl:grid-cols-6">
      <div>
        <label className="label">用户名</label>
        <input className="input w-full" value={username} onChange={(e) => setUsername(e.target.value)} required minLength={3} />
      </div>
      <div>
        <label className="label">初始密码</label>
        <input
          className="input w-full"
          type="text"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          required
          minLength={8}
          placeholder="至少 8 位，含字母数字"
        />
      </div>
      <div>
        <label className="label">昵称</label>
        <input className="input w-full" value={nickname} onChange={(e) => setNickname(e.target.value)} />
      </div>
      <div>
        <label className="label">邮箱（可选）</label>
        <input className="input w-full" type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
      </div>
      <div>
        <label className="label">角色</label>
        <select className="input w-full" value={role} onChange={(e) => setRole(e.target.value as UserRole)}>
          <option value="user">普通用户</option>
          <option value="admin">管理员</option>
        </select>
      </div>
      <div className="flex items-end">
        <button className="btn btn-primary w-full" disabled={create.isPending}>
          {create.isPending ? "创建中…" : "创建"}
        </button>
      </div>
    </form>
  );
}

function PasswordDialog({
  user,
  onClose,
  onDone,
}: {
  user: AuthUser;
  onClose: () => void;
  onDone: (message: string) => void;
}) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  const reset = useMutation({
    mutationFn: () => api.resetUserPassword(user.id, password),
    onSuccess: () => onDone(`已重置 ${user.username} 的密码，对方需重新登录`),
    onError: (err) => setError((err as Error).message),
  });

  return (
    <div className="fixed inset-0 z-30 flex items-center justify-center bg-black/60 px-4">
      <div className="w-full max-w-[360px] rounded-lg border border-slate-800 bg-slate-900 p-4">
        <div className="text-sm font-medium text-slate-200">重置密码 · {user.username}</div>
        <p className="mt-1 text-[11px] text-slate-500">重置后该账号所有已签发的登录状态立即失效。</p>
        <form
          className="mt-3 space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            setError("");
            reset.mutate();
          }}
        >
          <input
            className="input w-full"
            type="text"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="新密码：至少 8 位，含字母和数字"
            required
            minLength={8}
            autoFocus
          />
          {error && <div className="text-xs text-red-400">{error}</div>}
          <div className="flex justify-end gap-2">
            <button type="button" className="btn" onClick={onClose}>
              取消
            </button>
            <button type="submit" className="btn btn-primary" disabled={reset.isPending}>
              确认重置
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
