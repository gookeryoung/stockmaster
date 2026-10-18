import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";

type Mode = "login" | "register";
type FieldName = "username" | "email" | "password" | "confirm";
type FieldErrors = Partial<Record<FieldName, string>>;

const USERNAME_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{2,63}$/;
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+$/;
const PASSWORD_MIN = 8;
const PASSWORD_MAX = 128;
const PASSWORD_RULE = "密码至少 8 位，需同时包含字母和数字";

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
  if (value.length > 255 || value.startsWith("@") || value.endsWith("@") || !EMAIL_PATTERN.test(value)) {
    return "邮箱格式不正确";
  }
  return "";
}

function validatePassword(value: string): string {
  if (!value) return "请输入密码";
  if (value.length < PASSWORD_MIN || value.length > PASSWORD_MAX) return PASSWORD_RULE;
  if (value.trim() !== value) return "密码不能以空白字符开头或结尾";
  if (!/[A-Za-z]/.test(value) || !/\d/.test(value)) return PASSWORD_RULE;
  return "";
}

export default function Login() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const { config, user, login, register } = useAuth();

  const next = params.get("next") || "/";
  const canRegister = Boolean(config?.allow_register);
  const wechatEnabled = Boolean(config?.wechat_enabled);
  const gateOff = Boolean(config && !config.auth_enabled);
  const noUsers = Boolean(config && !config.has_users);
  const requestedMode: Mode = params.get("mode") === "register" ? "register" : "login";
  // 空库首次部署默认落在注册视图；显式 ?mode=login 或关闭注册时仍为登录。
  const mode: Mode = canRegister && (requestedMode === "register" || (params.get("mode") === null && noUsers))
    ? "register"
    : "login";

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [email, setEmail] = useState("");
  const [nickname, setNickname] = useState("");
  const [confirm, setConfirm] = useState("");
  const [errors, setErrors] = useState<FieldErrors>({});
  const [formError, setFormError] = useState(() => params.get("error") || "");
  const [busy, setBusy] = useState(false);
  const [wechatBusy, setWechatBusy] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirm, setShowConfirm] = useState(false);

  // 已登录（例如浏览器回退到登录页）直接回工作台，避免把用户困在登录页。
  useEffect(() => {
    if (user) navigate(next, { replace: true });
  }, [user, next, navigate]);

  // 视图与 URL 双向同步：默认视图也会写入 ?mode=，保证刷新/分享后仍在同一视图。
  useEffect(() => {
    if (!config) return;
    if (params.get("mode") === mode) return;
    const nextParams = new URLSearchParams(params);
    nextParams.set("mode", mode);
    setParams(nextParams, { replace: true });
  }, [config, mode, params, setParams]);

  // 微信回调失败会带 ?error= 回到登录页；进入页面或参数变化时展示。
  useEffect(() => {
    const urlError = params.get("error") || "";
    if (urlError) setFormError(urlError);
  }, [params]);

  const clearFieldError = (field: FieldName) => {
    setFormError("");
    setErrors((prev) => {
      if (!prev[field]) return prev;
      const nextErrors = { ...prev };
      delete nextErrors[field];
      return nextErrors;
    });
  };

  const switchMode = (nextMode: Mode) => {
    if (nextMode === "register" && !canRegister) return;
    setErrors({});
    setFormError("");
    const nextParams = new URLSearchParams(params);
    nextParams.set("mode", nextMode);
    nextParams.delete("error");
    setParams(nextParams, { replace: true });
  };

  const focusField = (field: FieldName) => {
    requestAnimationFrame(() => document.getElementById(field)?.focus());
  };

  const handleLogin = async (event: FormEvent) => {
    event.preventDefault();
    setFormError("");
    const nextErrors: FieldErrors = {};
    if (!username.trim()) nextErrors.username = "请输入用户名或邮箱";
    if (!password) nextErrors.password = "请输入密码";
    if (Object.keys(nextErrors).length > 0) {
      setErrors(nextErrors);
      focusField(nextErrors.username ? "username" : "password");
      return;
    }

    setErrors({});
    setBusy(true);
    try {
      await login(username.trim(), password);
      navigate(next, { replace: true });
    } catch (error) {
      setFormError((error as Error).message || "登录失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  };

  const handleRegister = async (event: FormEvent) => {
    event.preventDefault();
    setFormError("");
    const nextErrors: FieldErrors = {};
    const usernameError = validateUsername(username);
    if (usernameError) nextErrors.username = usernameError;
    const emailError = validateEmail(email);
    if (emailError) nextErrors.email = emailError;
    const passwordError = validatePassword(password);
    if (passwordError) nextErrors.password = passwordError;
    if (!confirm) nextErrors.confirm = "请再次输入密码";
    else if (confirm !== password) nextErrors.confirm = "两次输入的新密码不一致";

    const order: FieldName[] = ["username", "email", "password", "confirm"];
    const firstError = order.find((field) => nextErrors[field]);
    if (firstError) {
      setErrors(nextErrors);
      focusField(firstError);
      return;
    }

    setErrors({});
    setBusy(true);
    try {
      await register({
        username: username.trim(),
        password,
        email: email.trim() || undefined,
        nickname: nickname.trim() || undefined,
      });
      navigate(next, { replace: true });
    } catch (error) {
      const message = (error as Error).message || "注册失败，请稍后重试";
      // 账号冲突类错误定位到字段；其余（网络、限流、服务端异常）走表单顶部。
      if (message.includes("用户名")) {
        setErrors({ username: message });
        focusField("username");
      } else if (message.includes("邮箱")) {
        setErrors({ email: message });
        focusField("email");
      } else {
        setFormError(message);
      }
    } finally {
      setBusy(false);
    }
  };

  const startWechat = async () => {
    setFormError("");
    setWechatBusy(true);
    try {
      const { url } = await api.wechatUrl(next);
      window.location.assign(url);
    } catch (error) {
      setFormError((error as Error).message);
      setWechatBusy(false);
    }
  };

  const formErrorBanner = formError ? (
    <div
      role="alert"
      aria-live="polite"
      className="rounded-md border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs leading-relaxed text-red-300"
    >
      {formError}
    </div>
  ) : null;

  const wechatHint =
    config?.wechat_mode === "mock"
      ? "当前为模拟微信授权（SM_WECHAT_LOGIN_MODE=mock），不会访问真实微信"
      : config?.wechat_mode === "mp"
        ? "当前为公众号网页授权，请在微信中打开本页完成登录"
        : "将跳转到微信扫码页面，完成后自动返回";

  const passwordToggle = (
    <button
      type="button"
      onClick={() => setShowPassword((value) => !value)}
      aria-label={showPassword ? "隐藏密码" : "显示密码"}
      aria-pressed={showPassword}
      className="absolute inset-y-0 right-0 px-3 text-xs text-slate-400 transition hover:text-slate-200"
    >
      {showPassword ? "隐藏" : "显示"}
    </button>
  );

  return (
    <div className="flex min-h-full items-center justify-center bg-[#0b1120] px-4 py-10">
      <div className="w-full max-w-[400px]">
        <div className="mb-6 text-center">
          <div className="text-lg font-semibold tracking-wide text-slate-100">StockMaster</div>
          <div className="mt-1 text-xs text-slate-500">A股日线量化分析工作台</div>
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-5 shadow-2xl shadow-black/40">
          {!config && (
            <div className="mb-4 rounded-md border border-amber-900/60 bg-amber-950/40 px-3 py-2 text-xs leading-relaxed text-amber-300">
              无法获取登录配置，请检查后端连接后重试。
            </div>
          )}

          {gateOff && (
            <div className="mb-4 rounded-md border border-amber-900/60 bg-amber-950/40 px-3 py-2 text-xs leading-relaxed text-amber-300">
              服务器未开启登录校验（SM_AUTH_ENABLED=false），可直接进入工作台。
              登录后仍可使用账号相关功能。
            </div>
          )}

          {config && !canRegister && (
            <div className="mb-4 rounded-md border border-slate-800 bg-slate-950/60 px-3 py-2 text-xs leading-relaxed text-slate-400">
              本系统已关闭自助注册，请联系管理员开通账号。
              {!config.has_users && (
                <span className="mt-1 block text-amber-300">
                  系统尚无账号且自助注册已关闭，请用 SM_AUTH_ADMIN_USERNAME 或 CLI 创建管理员。
                </span>
              )}
            </div>
          )}

          {canRegister && (
            <div
              className="mb-4 grid grid-cols-2 gap-1 rounded-lg bg-slate-950/70 p-1"
              role="tablist"
              aria-label="登录或注册"
            >
              {(["login", "register"] as Mode[]).map((item) => (
                <button
                  key={item}
                  type="button"
                  role="tab"
                  aria-selected={mode === item}
                  onClick={() => switchMode(item)}
                  className={`rounded-md py-1.5 text-sm transition ${
                    mode === item ? "bg-sky-600 text-white" : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {item === "login" ? "登录" : "注册"}
                </button>
              ))}
            </div>
          )}

          {mode === "login" ? (
            <form className="space-y-3" onSubmit={handleLogin} noValidate aria-label="登录">
              <div>
                <label className="label" htmlFor="username">
                  用户名 / 邮箱 *
                </label>
                <input
                  id="username"
                  className={`input w-full ${errors.username ? "border-red-800" : ""}`}
                  value={username}
                  onChange={(e) => {
                    setUsername(e.target.value);
                    clearFieldError("username");
                  }}
                  autoComplete="username"
                  required
                  maxLength={255}
                  aria-invalid={Boolean(errors.username)}
                  aria-describedby={errors.username ? "username-error" : undefined}
                  placeholder="字母或数字开头，3-64 位"
                />
                {errors.username && (
                  <p id="username-error" role="alert" className="mt-1 text-[11px] text-red-300">
                    {errors.username}
                  </p>
                )}
              </div>

              <div>
                <label className="label" htmlFor="password">
                  密码 *
                </label>
                <div className="relative">
                  <input
                    id="password"
                    type={showPassword ? "text" : "password"}
                    className={`input w-full pr-14 ${errors.password ? "border-red-800" : ""}`}
                    value={password}
                    onChange={(e) => {
                      setPassword(e.target.value);
                      clearFieldError("password");
                    }}
                    autoComplete="current-password"
                    required
                    maxLength={128}
                    aria-invalid={Boolean(errors.password)}
                    aria-describedby={errors.password ? "password-error" : undefined}
                  />
                  {passwordToggle}
                </div>
                {errors.password && (
                  <p id="password-error" role="alert" className="mt-1 text-[11px] text-red-300">
                    {errors.password}
                  </p>
                )}
              </div>

              {formErrorBanner}

              <button type="submit" className="btn btn-primary w-full py-2" disabled={busy}>
                {busy ? "登录中…" : "登录"}
              </button>
            </form>
          ) : (
            <form className="space-y-3" onSubmit={handleRegister} noValidate aria-label="注册">
              {noUsers && (
                <p className="text-center text-[11px] leading-relaxed text-amber-300/90">
                  首个账号将成为管理员，请妥善保管密码。
                </p>
              )}

              <div>
                <label className="label" htmlFor="username">
                  用户名 *
                </label>
                <input
                  id="username"
                  className={`input w-full ${errors.username ? "border-red-800" : ""}`}
                  value={username}
                  onChange={(e) => {
                    setUsername(e.target.value);
                    clearFieldError("username");
                  }}
                  autoComplete="username"
                  required
                  maxLength={64}
                  aria-invalid={Boolean(errors.username)}
                  aria-describedby={errors.username ? "username-error" : undefined}
                  placeholder="3-64 位，字母或数字开头"
                />
                {errors.username && (
                  <p id="username-error" role="alert" className="mt-1 text-[11px] text-red-300">
                    {errors.username}
                  </p>
                )}
              </div>

              <div>
                <label className="label" htmlFor="nickname">
                  昵称（可选）
                </label>
                <input
                  id="nickname"
                  className="input w-full"
                  value={nickname}
                  onChange={(e) => setNickname(e.target.value)}
                  maxLength={64}
                  autoComplete="nickname"
                  placeholder="留空则使用用户名"
                />
              </div>

              <div>
                <label className="label" htmlFor="email">
                  邮箱（可选，用于登录）
                </label>
                <input
                  id="email"
                  type="email"
                  className={`input w-full ${errors.email ? "border-red-800" : ""}`}
                  value={email}
                  onChange={(e) => {
                    setEmail(e.target.value);
                    clearFieldError("email");
                  }}
                  maxLength={255}
                  autoComplete="email"
                  aria-invalid={Boolean(errors.email)}
                  aria-describedby={errors.email ? "email-error" : undefined}
                  placeholder="不用于找回密码"
                />
                {errors.email && (
                  <p id="email-error" role="alert" className="mt-1 text-[11px] text-red-300">
                    {errors.email}
                  </p>
                )}
              </div>

              <div>
                <label className="label" htmlFor="password">
                  密码 *
                </label>
                <div className="relative">
                  <input
                    id="password"
                    type={showPassword ? "text" : "password"}
                    className={`input w-full pr-14 ${errors.password ? "border-red-800" : ""}`}
                    value={password}
                    onChange={(e) => {
                      setPassword(e.target.value);
                      clearFieldError("password");
                    }}
                    autoComplete="new-password"
                    required
                    maxLength={128}
                    aria-invalid={Boolean(errors.password)}
                    aria-describedby={errors.password ? "password-error" : "password-rule"}
                    placeholder="至少 8 位，需同时包含字母和数字"
                  />
                  {passwordToggle}
                </div>
                <p id="password-rule" className="mt-1 text-[11px] text-slate-500">
                  至少 8 位，需同时包含字母和数字，且不能以空白字符开头或结尾。
                </p>
                {errors.password && (
                  <p id="password-error" role="alert" className="mt-1 text-[11px] text-red-300">
                    {errors.password}
                  </p>
                )}
              </div>

              <div>
                <label className="label" htmlFor="confirm">
                  确认密码 *
                </label>
                <div className="relative">
                  <input
                    id="confirm"
                    type={showConfirm ? "text" : "password"}
                    className={`input w-full pr-14 ${errors.confirm ? "border-red-800" : ""}`}
                    value={confirm}
                    onChange={(e) => {
                      setConfirm(e.target.value);
                      clearFieldError("confirm");
                    }}
                    autoComplete="new-password"
                    required
                    maxLength={128}
                    aria-invalid={Boolean(errors.confirm)}
                    aria-describedby={errors.confirm ? "confirm-error" : undefined}
                    placeholder="再次输入密码"
                  />
                  <button
                    type="button"
                    onClick={() => setShowConfirm((value) => !value)}
                    aria-label={showConfirm ? "隐藏确认密码" : "显示确认密码"}
                    aria-pressed={showConfirm}
                    className="absolute inset-y-0 right-0 px-3 text-xs text-slate-400 transition hover:text-slate-200"
                  >
                    {showConfirm ? "隐藏" : "显示"}
                  </button>
                </div>
                {errors.confirm && (
                  <p id="confirm-error" role="alert" className="mt-1 text-[11px] text-red-300">
                    {errors.confirm}
                  </p>
                )}
              </div>

              {formErrorBanner}

              <button type="submit" className="btn btn-primary w-full py-2" disabled={busy}>
                {busy ? "注册中…" : "注册并登录"}
              </button>
            </form>
          )}

          {mode === "login" && wechatEnabled && (
            <div className="mt-4">
              <div className="mb-3 flex items-center gap-2 text-[11px] text-slate-600">
                <span className="h-px flex-1 bg-slate-800" />
                或
                <span className="h-px flex-1 bg-slate-800" />
              </div>
              <button
                type="button"
                onClick={startWechat}
                disabled={wechatBusy}
                className="flex w-full items-center justify-center gap-2 rounded-md bg-[#07c160] py-2 text-sm font-medium text-white transition hover:bg-[#06ad56] disabled:opacity-50"
              >
                <svg viewBox="0 0 24 24" className="h-4 w-4" fill="currentColor" aria-hidden="true">
                  <path d="M8.7 4C4.9 4 1.8 6.6 1.8 9.8c0 1.8 1 3.4 2.6 4.5l-.6 2 2.3-1.2c.8.2 1.6.4 2.5.4h.6a4.9 4.9 0 0 1-.2-1.3c0-2.9 2.8-5.2 6.2-5.2h.5C15.1 6 12.2 4 8.7 4Zm-2.4 3a1 1 0 1 1 0 2 1 1 0 0 1 0-2Zm4.8 0a1 1 0 1 1 0 2 1 1 0 0 1 0-2Z" />
                  <path d="M22.2 14.2c0-2.6-2.5-4.7-5.6-4.7s-5.6 2.1-5.6 4.7 2.5 4.7 5.6 4.7c.7 0 1.4-.1 2-.3l1.9 1-.5-1.7c1.4-.9 2.2-2.2 2.2-3.7Zm-7.4-1.2a.9.9 0 1 1 0-1.7.9.9 0 0 1 0 1.7Zm3.7 0a.9.9 0 1 1 0-1.7.9.9 0 0 1 0 1.7Z" />
                </svg>
                {wechatBusy ? "跳转中…" : "微信扫码登录"}
              </button>
              <p className={`mt-2 text-center text-[11px] ${config?.wechat_mode === "mock" ? "text-amber-500/80" : "text-slate-500"}`}>
                {wechatHint}
              </p>
            </div>
          )}

          {mode === "login" && canRegister && noUsers && (
            <p className="mt-4 text-center text-[11px] leading-relaxed text-slate-500">
              系统还没有账号，第一个注册的用户将成为<strong className="text-slate-300">管理员</strong>。
            </p>
          )}
        </div>

        <div className="mt-4 text-center text-[11px] text-slate-600">
          {gateOff ? (
            <Link to="/" className="text-sky-400 hover:text-sky-300">
              直接进入工作台 →
            </Link>
          ) : (
            <span>本工作台仅供研究参考，不构成投资建议</span>
          )}
        </div>
      </div>
    </div>
  );
}
