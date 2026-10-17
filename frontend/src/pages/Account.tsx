import { FormEvent, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import { formatDateTime } from "../format";
import { ROLE_LABEL } from "../workspace";

export default function Account() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { user, config, setUser, logout, refreshUser } = useAuth();

  const [nickname, setNickname] = useState(user?.nickname ?? "");
  const [email, setEmail] = useState(user?.email ?? "");
  const [feedback, setFeedback] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  useEffect(() => {
    setNickname(user?.nickname ?? "");
    setEmail(user?.email ?? "");
  }, [user?.nickname, user?.email]);

  useEffect(() => {
    if (params.get("bound") === "1") {
      setFeedback({ kind: "ok", text: "微信已绑定，下次可直接扫码登录" });
      refreshUser();
    }
  }, [params, refreshUser]);

  const saveProfile = useMutation({
    mutationFn: () => api.updateMe({ nickname: nickname.trim(), email: email.trim() }),
    onSuccess: (updated) => {
      setUser(updated);
      setFeedback({ kind: "ok", text: "资料已保存" });
    },
    onError: (error) => setFeedback({ kind: "err", text: (error as Error).message }),
  });

  const changePassword = useMutation({
    mutationFn: ({ oldPassword, newPassword }: { oldPassword: string; newPassword: string }) =>
      api.changePassword(oldPassword, newPassword),
    onSuccess: (result) => {
      // Changing the password rotates the token server-side; keep this tab logged in.
      useAuth.getState().applyToken(result.token, result.user);
      setFeedback({ kind: "ok", text: "密码已更新，其他设备需重新登录" });
    },
    onError: (error) => setFeedback({ kind: "err", text: (error as Error).message }),
  });

  const unbind = useMutation({
    mutationFn: (id: number) => api.unbindIdentity(id),
    onSuccess: async () => {
      await refreshUser();
      setFeedback({ kind: "ok", text: "已解绑" });
    },
    onError: (error) => setFeedback({ kind: "err", text: (error as Error).message }),
  });

  const bindWechat = async () => {
    try {
      const { url } = await api.wechatUrl("/account", true);
      window.location.assign(url);
    } catch (error) {
      setFeedback({ kind: "err", text: (error as Error).message });
    }
  };

  // 我的数据概览：隔离关闭时没有「个人工作区」概念，因此不查（后端也不会返回该字段）。
  const workspaces = useQuery({
    queryKey: ["account-workspaces"],
    queryFn: () => api.workspaces(),
    enabled: Boolean(config?.workspace_enabled),
  });

  if (!user) {
    return (
      <div className="card text-sm text-slate-400">
        当前未登录。
        <button className="btn btn-primary ml-2" onClick={() => navigate("/login")}>
          去登录
        </button>
      </div>
    );
  }

  const wechatIdentities = user.identities.filter((item) => item.provider === "wechat");
  const wechatAvailable = Boolean(config?.wechat_enabled);

  const workspaceList = workspaces.data ?? [];
  const personalTotal = workspaceList
    .filter((row) => row.kind === "personal")
    .reduce((sum, row) => sum + row.data_total, 0);
  const accessibleTotal = workspaceList.reduce((sum, row) => sum + row.data_total, 0);

  return (
    <div className="space-y-4">
      <div className="card space-y-3">
        <div className="flex flex-wrap items-center gap-3">
          <div className="flex h-11 w-11 items-center justify-center rounded-full bg-sky-900/50 text-base text-sky-300">
            {(user.nickname || user.username).slice(0, 1).toUpperCase()}
          </div>
          <div>
            <div className="text-sm font-medium text-slate-100">
              {user.nickname || user.username}
              <span className="badge ml-2 bg-slate-800 text-slate-300">
                {user.role === "admin" ? "管理员" : "普通用户"}
              </span>
            </div>
            <div className="text-[11px] text-slate-500">
              {user.username}
              {user.email ? ` · ${user.email}` : ""} · 注册于 {formatDateTime(user.created_at)}
            </div>
          </div>
          <button
            className="btn ml-auto"
            onClick={async () => {
              await logout();
              navigate("/login", { replace: true });
            }}
          >
            退出登录
          </button>
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
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <form
          className="card space-y-3"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            saveProfile.mutate();
          }}
        >
          <div className="text-sm font-medium text-slate-300">基本资料</div>
          <div>
            <label className="label">昵称</label>
            <input className="input w-full" value={nickname} onChange={(e) => setNickname(e.target.value)} maxLength={64} />
          </div>
          <div>
            <label className="label">邮箱</label>
            <input
              className="input w-full"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="用于登录与找回"
            />
          </div>
          <button className="btn btn-primary" disabled={saveProfile.isPending}>
            {saveProfile.isPending ? "保存中…" : "保存"}
          </button>
        </form>

        <form
          className="card space-y-3"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            const form = event.currentTarget as HTMLFormElement;
            const data = new FormData(form);
            const oldPassword = String(data.get("old") ?? "");
            const newPassword = String(data.get("new") ?? "");
            const repeat = String(data.get("repeat") ?? "");
            if (newPassword !== repeat) {
              setFeedback({ kind: "err", text: "两次输入的新密码不一致" });
              return;
            }
            changePassword.mutate({ oldPassword, newPassword });
            form.reset();
          }}
        >
          <div className="text-sm font-medium text-slate-300">
            {user.has_password ? "修改密码" : "设置密码"}
          </div>
          {!user.has_password && (
            <p className="text-[11px] text-amber-500/90">
              当前账号通过微信登录，尚未设置密码。设置后即可用用户名 + 密码登录。
            </p>
          )}
          {user.has_password && (
            <div>
              <label className="label">原密码</label>
              <input className="input w-full" name="old" type="password" autoComplete="current-password" required />
            </div>
          )}
          <div>
            <label className="label">新密码</label>
            <input
              className="input w-full"
              name="new"
              type="password"
              autoComplete="new-password"
              minLength={8}
              required
              placeholder="至少 8 位，含字母和数字"
            />
          </div>
          <div>
            <label className="label">确认新密码</label>
            <input className="input w-full" name="repeat" type="password" autoComplete="new-password" minLength={8} required />
          </div>
          <button className="btn btn-primary" disabled={changePassword.isPending}>
            {changePassword.isPending ? "提交中…" : "更新密码"}
          </button>
        </form>
      </div>

      <div className="card space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="text-sm font-medium text-slate-300">我的数据概览</div>
          <span className="text-[11px] text-slate-500">只读 · 统计你参与的工作区</span>
        </div>

        {!config?.workspace_enabled && (
          <p className="text-xs text-slate-500">未启用数据隔离，数据为全站共享，不区分个人工作区。</p>
        )}

        {config?.workspace_enabled && workspaces.isLoading && (
          <p className="text-xs text-slate-500">数据概览加载中…</p>
        )}

        {config?.workspace_enabled && workspaces.isError && (
          <p className="text-xs text-slate-500">数据概览暂不可用，不影响其他功能。</p>
        )}

        {config?.workspace_enabled && workspaces.data && (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <span className="badge bg-slate-800 text-slate-300">
                参与工作区 {workspaceList.length} 个
              </span>
              <span className="badge bg-slate-800 text-slate-300">
                个人工作区数据 {personalTotal} 条
              </span>
              <span className="badge bg-slate-800 text-slate-300">
                可访问数据合计 {accessibleTotal} 条
              </span>
            </div>

            <div className="table-wrap">
              <table className="w-full">
                <thead>
                  <tr className="border-b border-slate-800">
                    <th className="th">工作区</th>
                    <th className="th">类型</th>
                    <th className="th">我的角色</th>
                    <th className="th">成员</th>
                    <th className="th">数据量</th>
                  </tr>
                </thead>
                <tbody>
                  {workspaceList.map((row) => (
                    <tr key={row.id} className="border-b border-slate-800/50 last:border-0">
                      <td className="td font-medium text-slate-100">{row.name}</td>
                      <td className="td text-slate-400">{row.kind === "personal" ? "个人" : "共享"}</td>
                      <td className="td text-slate-300">{ROLE_LABEL[row.role] ?? row.role}</td>
                      <td className="td text-slate-400">{row.member_count}</td>
                      <td className="td text-slate-300">{row.data_total} 条</td>
                    </tr>
                  ))}
                  {workspaceList.length === 0 && (
                    <tr>
                      <td className="td text-slate-500" colSpan={5}>
                        还没有加入任何工作区。
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </>
        )}
      </div>

      <div className="card space-y-3">
        <div className="flex items-center gap-2">
          <div className="text-sm font-medium text-slate-300">第三方账号</div>
          {!wechatAvailable && <span className="text-[11px] text-slate-500">服务端未配置微信登录</span>}
        </div>

        {wechatIdentities.map((identity) => (
          <div
            key={identity.id}
            className="flex flex-wrap items-center gap-3 rounded-md border border-slate-800 bg-slate-950/40 px-3 py-2"
          >
            <span className="badge bg-[#07c160]/15 text-[#07c160]">微信</span>
            <span className="text-sm text-slate-200">{identity.nickname || "已绑定"}</span>
            <span className="text-[11px] text-slate-500">绑定于 {formatDateTime(identity.created_at)}</span>
            <button
              className="btn ml-auto px-2 py-1 text-xs hover:border-red-700 hover:bg-red-950/60"
              disabled={unbind.isPending}
              onClick={() => {
                if (window.confirm("解绑后将无法用该微信登录，确认解绑？")) unbind.mutate(identity.id);
              }}
            >
              解绑
            </button>
          </div>
        ))}

        {wechatIdentities.length === 0 && (
          <div className="flex flex-wrap items-center gap-3 rounded-md border border-slate-800 bg-slate-950/40 px-3 py-2">
            <span className="text-sm text-slate-400">尚未绑定微信</span>
            <button className="btn ml-auto px-2 py-1 text-xs" disabled={!wechatAvailable} onClick={bindWechat}>
              绑定微信
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
