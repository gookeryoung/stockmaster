/**
 * 账号设置页：身份摘要 → 基本资料 → 安全（密码）→ 第三方账号 → 我的数据概览。
 * 设计依据 docs/UI_AUTH.md §4.4 / §8。
 *
 * 所有写操作都需要服务端确认：前端只做减少误操作与即时校验，最终以 403/409/422 为准。
 */

import { FormEvent, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import { formatDateTime } from "../format";
import { ROLE_LABEL } from "../workspace";

const PASSWORD_MIN = 8;
const PASSWORD_MAX = 128;
const PASSWORD_RULE = "密码至少 8 位，需同时包含字母和数字";

type Notice = { kind: "ok" | "err"; text: string };
type FieldErrors = {
  nickname?: string;
  email?: string;
  oldPassword?: string;
  newPassword?: string;
  confirmPassword?: string;
};

function validateEmail(raw: string): string {
  const value = raw.trim();
  if (!value) return "";
  if (value.length > 255 || value.startsWith("@") || value.endsWith("@") || !/^[^\s@]+@[^\s@]+$/.test(value)) {
    return "邮箱格式不正确";
  }
  return "";
}

function validateNewPassword(value: string): string {
  if (!value) return "请输入新密码";
  if (value.length < PASSWORD_MIN || value.length > PASSWORD_MAX) return PASSWORD_RULE;
  if (value.trim() !== value) return "密码不能以空白字符开头或结尾";
  if (!/[A-Za-z]/.test(value) || !/\d/.test(value)) return PASSWORD_RULE;
  return "";
}

export default function Account() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { user, config, setUser, logout, refreshUser } = useAuth();

  const [nickname, setNickname] = useState(user?.nickname ?? "");
  const [email, setEmail] = useState(user?.email ?? "");
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showNewPassword, setShowNewPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const [errors, setErrors] = useState<FieldErrors>({});
  const [feedback, setFeedback] = useState<Notice | null>(null);

  useEffect(() => {
    setNickname(user?.nickname ?? "");
    setEmail(user?.email ?? "");
  }, [user?.nickname, user?.email]);

  useEffect(() => {
    if (params.get("bound") !== "1") return;
    setFeedback({ kind: "ok", text: "微信已绑定，下次可直接扫码登录。" });
    void refreshUser();
  }, [params, refreshUser]);

  const saveProfile = useMutation({
    mutationFn: () => api.updateMe({ nickname: nickname.trim(), email: email.trim() }),
    onSuccess: (updated) => {
      setUser(updated);
      setErrors((prev) => ({ ...prev, email: undefined }));
      setFeedback({ kind: "ok", text: "资料已保存。" });
    },
    onError: (error) => {
      const message = (error as Error).message;
      if (message.includes("邮箱")) {
        setErrors((prev) => ({ ...prev, email: message }));
      } else {
        setFeedback({ kind: "err", text: message });
      }
    },
  });

  const changePassword = useMutation({
    mutationFn: () => api.changePassword(oldPassword, newPassword),
    onSuccess: (result) => {
      // 改密会递增 token_version，服务端签发新 token；应用它保持本页在线。
      useAuth.getState().applyToken(result.token, result.user);
      setOldPassword("");
      setNewPassword("");
      setConfirmPassword("");
      setErrors({});
      setFeedback({
        kind: "ok",
        text: "密码已更新。其他设备已退出登录，本页面保持在线。",
      });
    },
    onError: (error) => {
      const message = (error as Error).message;
      if (message.includes("原密码")) {
        setErrors((prev) => ({ ...prev, oldPassword: message }));
      } else if (message.includes("密码")) {
        setErrors((prev) => ({ ...prev, newPassword: message }));
      } else {
        setFeedback({ kind: "err", text: message });
      }
    },
  });

  const unbind = useMutation({
    mutationFn: (id: number) => api.unbindIdentity(id),
    onSuccess: async () => {
      await refreshUser();
      setFeedback({ kind: "ok", text: "微信已解绑。" });
    },
    onError: (error) => setFeedback({ kind: "err", text: (error as Error).message }),
  });

  // 我的数据概览：隔离关闭时没有「个人工作区」概念，不发起请求。
  const workspaces = useQuery({
    queryKey: ["account-workspaces"],
    queryFn: () => api.workspaces(),
    enabled: Boolean(config?.workspace_enabled),
  });

  if (!user) {
    return (
      <div className="card space-y-2">
        <p className="text-sm text-slate-400">当前未登录。</p>
        <p className="text-xs text-slate-500">
          本机模式无需登录即可使用工作台；登录后可管理账号与微信绑定。
        </p>
        <div>
          <button type="button" className="btn btn-primary" onClick={() => navigate("/login")}>
            去登录
          </button>
        </div>
      </div>
    );
  }

  const wechatIdentities = user.identities.filter((item) => item.provider === "wechat");
  const wechatAvailable = Boolean(config?.wechat_enabled);
  const loginMethod = user.has_password
    ? wechatIdentities.length > 0
      ? "密码 + 微信"
      : "用户名 + 密码"
    : wechatIdentities.length > 0
      ? "仅微信扫码"
      : "—";
  const unbindBlocked = !user.has_password && wechatIdentities.length <= 1;
  const workspaceEnabled = Boolean(config?.workspace_enabled);
  const workspaceList = workspaces.data ?? [];
  const personalTotal = workspaceList
    .filter((row) => row.kind === "personal")
    .reduce((sum, row) => sum + row.data_total, 0);
  const accessibleTotal = workspaceList.reduce((sum, row) => sum + row.data_total, 0);

  const focusField = (field: keyof FieldErrors) => {
    requestAnimationFrame(() => document.getElementById(`account-${field}`)?.focus());
  };

  const submitProfile = () => {
    setFeedback(null);
    const nextErrors: FieldErrors = {};
    const emailError = validateEmail(email);
    if (emailError) nextErrors.email = emailError;
    if (Object.keys(nextErrors).length > 0) {
      setErrors(nextErrors);
      focusField("email");
      return;
    }
    setErrors((prev) => ({ ...prev, email: undefined }));
    saveProfile.mutate();
  };

  const submitPassword = () => {
    setFeedback(null);
    const nextErrors: FieldErrors = {};
    if (user.has_password && !oldPassword) nextErrors.oldPassword = "请输入原密码";
    const newError = validateNewPassword(newPassword);
    if (newError) nextErrors.newPassword = newError;
    if (!confirmPassword) nextErrors.confirmPassword = "请再次输入新密码";
    else if (confirmPassword !== newPassword) nextErrors.confirmPassword = "两次输入的新密码不一致";
    const first = (["oldPassword", "newPassword", "confirmPassword"] as const).find(
      (field) => nextErrors[field],
    );
    if (first) {
      setErrors(nextErrors);
      focusField(first);
      return;
    }
    setErrors({});
    changePassword.mutate();
  };

  const bindWechat = async () => {
    try {
      const { url } = await api.wechatUrl("/account", true);
      window.location.assign(url);
    } catch (error) {
      setFeedback({ kind: "err", text: (error as Error).message });
    }
  };

  const passwordRules = [
    { label: "8–128 位", ok: newPassword.length >= PASSWORD_MIN && newPassword.length <= PASSWORD_MAX },
    { label: "无首尾空白", ok: newPassword.length > 0 && newPassword.trim() === newPassword },
    { label: "同时包含字母和数字", ok: /[A-Za-z]/.test(newPassword) && /\d/.test(newPassword) },
  ];

  return (
    <div className="space-y-4">
      {/* 身份摘要 */}
      <div className="card space-y-3">
        <div className="flex flex-wrap items-start gap-3">
          {user.avatar_url ? (
            <img
              src={user.avatar_url}
              alt=""
              className="h-11 w-11 rounded-full object-cover"
            />
          ) : (
            <div className="flex h-11 w-11 items-center justify-center rounded-full bg-sky-900/50 text-base text-sky-300">
              {(user.nickname || user.username).slice(0, 1).toUpperCase()}
            </div>
          )}
          <div className="min-w-0 flex-1">
            <div className="text-sm font-medium text-slate-100">
              {user.nickname || user.username}
              <span className="badge ml-2 bg-slate-800 text-slate-300">
                {user.role === "admin" ? "管理员" : "普通用户"}
              </span>
            </div>
            <div className="text-[11px] text-slate-500">
              {user.username}
              {user.email ? ` · ${user.email}` : " · 未填写邮箱"} · 注册于 {formatDateTime(user.created_at)}
            </div>
          </div>
          <button
            type="button"
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
      </div>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {/* 基本资料 */}
        <form
          className="card space-y-3"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            submitProfile();
          }}
          noValidate
          aria-label="基本资料"
        >
          <div className="text-sm font-medium text-slate-300">基本资料</div>
          <div>
            <label className="label" htmlFor="account-nickname">
              昵称
            </label>
            <input
              id="account-nickname"
              className="input w-full"
              value={nickname}
              onChange={(event) => setNickname(event.target.value)}
              maxLength={64}
              autoComplete="nickname"
              placeholder="留空则显示用户名"
            />
          </div>
          <div>
            <label className="label" htmlFor="account-email">
              邮箱
            </label>
            <input
              id="account-email"
              className={`input w-full ${errors.email ? "border-red-800" : ""}`}
              type="email"
              value={email}
              onChange={(event) => {
                setEmail(event.target.value);
                setErrors((prev) => ({ ...prev, email: undefined }));
              }}
              maxLength={255}
              autoComplete="email"
              aria-invalid={Boolean(errors.email)}
              aria-describedby={errors.email ? "account-email-error" : undefined}
              placeholder="用于登录，不承诺找回密码"
            />
            {errors.email && (
              <p id="account-email-error" role="alert" className="mt-1 text-[11px] text-red-300">
                {errors.email}
              </p>
            )}
          </div>
          <button
            type="submit"
            className="btn btn-primary"
            disabled={saveProfile.isPending}
          >
            {saveProfile.isPending ? "保存中…" : "保存"}
          </button>
        </form>

        {/* 安全 */}
        <form
          className="card space-y-3"
          onSubmit={(event: FormEvent) => {
            event.preventDefault();
            submitPassword();
          }}
          noValidate
          aria-label="安全设置"
        >
          <div className="flex flex-wrap items-center gap-2">
            <div className="text-sm font-medium text-slate-300">
              {user.has_password ? "修改密码" : "设置密码"}
            </div>
            <span className="badge bg-slate-800 text-slate-300">当前登录方式：{loginMethod}</span>
          </div>
          {!user.has_password && (
            <p className="text-[11px] leading-relaxed text-amber-300/90">
              当前账号通过微信登录，尚未设置密码。设置后即可用用户名 + 密码登录。
            </p>
          )}
          {user.has_password && (
            <div>
              <label className="label" htmlFor="account-oldPassword">
                原密码 *
              </label>
              <input
                id="account-oldPassword"
                className={`input w-full ${errors.oldPassword ? "border-red-800" : ""}`}
                type="password"
                value={oldPassword}
                onChange={(event) => {
                  setOldPassword(event.target.value);
                  setErrors((prev) => ({ ...prev, oldPassword: undefined }));
                }}
                autoComplete="current-password"
                aria-invalid={Boolean(errors.oldPassword)}
                aria-describedby={errors.oldPassword ? "account-oldPassword-error" : undefined}
              />
              {errors.oldPassword && (
                <p id="account-oldPassword-error" role="alert" className="mt-1 text-[11px] text-red-300">
                  {errors.oldPassword}
                </p>
              )}
            </div>
          )}
          <div>
            <label className="label" htmlFor="account-newPassword">
              新密码 *
            </label>
            <div className="relative">
              <input
                id="account-newPassword"
                className={`input w-full pr-14 ${errors.newPassword ? "border-red-800" : ""}`}
                type={showNewPassword ? "text" : "password"}
                value={newPassword}
                onChange={(event) => {
                  setNewPassword(event.target.value);
                  setErrors((prev) => ({ ...prev, newPassword: undefined }));
                }}
                autoComplete="new-password"
                aria-invalid={Boolean(errors.newPassword)}
                aria-describedby={errors.newPassword ? "account-newPassword-error" : "account-password-rules"}
              />
              <button
                type="button"
                onClick={() => setShowNewPassword((value) => !value)}
                aria-label={showNewPassword ? "隐藏新密码" : "显示新密码"}
                aria-pressed={showNewPassword}
                className="absolute inset-y-0 right-0 px-3 text-xs text-slate-400 transition hover:text-slate-200"
              >
                {showNewPassword ? "隐藏" : "显示"}
              </button>
            </div>
            <ul id="account-password-rules" className="mt-1 space-y-0.5 text-[11px]" aria-label="新密码规则">
              {passwordRules.map((rule) => (
                <li key={rule.label} className={rule.ok ? "text-emerald-300" : "text-slate-500"}>
                  {rule.ok ? "✓" : "○"} {rule.label}
                </li>
              ))}
            </ul>
            {errors.newPassword && (
              <p id="account-newPassword-error" role="alert" className="mt-1 text-[11px] text-red-300">
                {errors.newPassword}
              </p>
            )}
          </div>
          <div>
            <label className="label" htmlFor="account-confirmPassword">
              确认新密码 *
            </label>
            <div className="relative">
              <input
                id="account-confirmPassword"
                className={`input w-full pr-14 ${errors.confirmPassword ? "border-red-800" : ""}`}
                type={showConfirmPassword ? "text" : "password"}
                value={confirmPassword}
                onChange={(event) => {
                  setConfirmPassword(event.target.value);
                  setErrors((prev) => ({ ...prev, confirmPassword: undefined }));
                }}
                autoComplete="new-password"
                aria-invalid={Boolean(errors.confirmPassword)}
                aria-describedby={errors.confirmPassword ? "account-confirmPassword-error" : undefined}
              />
              <button
                type="button"
                onClick={() => setShowConfirmPassword((value) => !value)}
                aria-label={showConfirmPassword ? "隐藏确认密码" : "显示确认密码"}
                aria-pressed={showConfirmPassword}
                className="absolute inset-y-0 right-0 px-3 text-xs text-slate-400 transition hover:text-slate-200"
              >
                {showConfirmPassword ? "隐藏" : "显示"}
              </button>
            </div>
            {errors.confirmPassword && (
              <p id="account-confirmPassword-error" role="alert" className="mt-1 text-[11px] text-red-300">
                {errors.confirmPassword}
              </p>
            )}
          </div>
          <button type="submit" className="btn btn-primary" disabled={changePassword.isPending}>
            {changePassword.isPending ? "提交中…" : user.has_password ? "更新密码" : "设置密码"}
          </button>
        </form>
      </div>

      {/* 第三方账号 */}
      <div className="card space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="text-sm font-medium text-slate-300">第三方账号</div>
          {!wechatAvailable && (
            <span className="text-[11px] text-slate-500">
              服务端未配置微信登录（SM_WECHAT_APP_ID / SECRET），暂不可绑定。
            </span>
          )}
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
              type="button"
              className="btn ml-auto px-2 py-1 text-xs hover:border-red-700 hover:bg-red-950/60"
              disabled={unbind.isPending || unbindBlocked}
              title={
                unbindBlocked
                  ? "这是账号唯一的登录方式，请先设置密码再解绑"
                  : user.has_password
                    ? "解绑后将无法用该微信登录，但仍可用密码登录"
                    : "解绑后将无法用该微信登录"
              }
              onClick={() => {
                if (
                  window.confirm(
                    user.has_password
                      ? "解绑后将无法用该微信登录，确认解绑？"
                      : "解绑后将无法用该微信登录；若未设置密码将无法登录。确认解绑？",
                  )
                ) {
                  unbind.mutate(identity.id);
                }
              }}
            >
              解绑
            </button>
          </div>
        ))}

        {wechatIdentities.length === 0 && (
          <div className="flex flex-wrap items-center gap-3 rounded-md border border-slate-800 bg-slate-950/40 px-3 py-2">
            <span className="text-sm text-slate-400">尚未绑定微信</span>
            <button
              type="button"
              className="btn ml-auto px-2 py-1 text-xs"
              disabled={!wechatAvailable}
              title={!wechatAvailable ? "服务端未配置微信登录" : "绑定后可用微信扫码登录"}
              onClick={() => void bindWechat()}
            >
              绑定微信
            </button>
          </div>
        )}

        {unbindBlocked && (
          <p className="text-[11px] leading-relaxed text-amber-300/90">
            这是账号唯一的登录方式，请先在上方「设置密码」，再解绑微信。
          </p>
        )}
      </div>

      {/* 我的数据概览（只读） */}
      <div className="card space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="text-sm font-medium text-slate-300">我的数据概览</div>
          <span className="text-[11px] text-slate-500">只读 · 统计你参与的工作区</span>
        </div>

        {!workspaceEnabled && (
          <p className="text-xs text-slate-500">未启用数据隔离，数据为全站共享，不区分个人工作区。</p>
        )}

        {workspaceEnabled && workspaces.isLoading && (
          <div className="space-y-1" aria-label="数据概览加载中">
            {[0, 1, 2].map((index) => (
              <div key={index} className="h-4 animate-pulse rounded bg-slate-800/60" />
            ))}
          </div>
        )}

        {workspaceEnabled && workspaces.isError && (
          <div className="flex flex-wrap items-center gap-2 text-xs text-amber-300">
            数据概览暂不可用，不影响其他功能。
            <button type="button" className="btn px-2 py-1 text-xs" onClick={() => void workspaces.refetch()}>
              重试
            </button>
          </div>
        )}

        {workspaceEnabled && workspaces.data && (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <span className="badge bg-slate-800 text-slate-300">参与工作区 {workspaceList.length} 个</span>
              <span className="badge bg-slate-800 text-slate-300">个人工作区数据 {personalTotal} 条</span>
              <span className="badge bg-slate-800 text-slate-300">可访问数据合计 {accessibleTotal} 条</span>
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
    </div>
  );
}
