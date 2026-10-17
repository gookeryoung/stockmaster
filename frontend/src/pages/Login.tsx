import { FormEvent, useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";

type Mode = "login" | "register";

export default function Login() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { config, user, login, register } = useAuth();

  const next = params.get("next") || "/";
  const callbackError = params.get("error") || "";

  const [mode, setMode] = useState<Mode>("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [email, setEmail] = useState("");
  const [nickname, setNickname] = useState("");
  const [error, setError] = useState(callbackError);
  const [busy, setBusy] = useState(false);
  const [wechatBusy, setWechatBusy] = useState(false);

  // Arriving here already authenticated (e.g. back button) should not trap the user.
  useEffect(() => {
    if (user) navigate(next, { replace: true });
  }, [user, next, navigate]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setError("");
    setBusy(true);
    try {
      if (mode === "login") {
        await login(username.trim(), password);
      } else {
        await register({
          username: username.trim(),
          password,
          email: email.trim() || undefined,
          nickname: nickname.trim() || undefined,
        });
      }
      navigate(next, { replace: true });
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const startWechat = async () => {
    setError("");
    setWechatBusy(true);
    try {
      const { url } = await api.wechatUrl(next);
      window.location.assign(url);
    } catch (err) {
      setError((err as Error).message);
      setWechatBusy(false);
    }
  };

  const gateOff = config && !config.auth_enabled;
  const canRegister = Boolean(config?.allow_register);
  const wechatEnabled = Boolean(config?.wechat_enabled);
  const showModeTabs = canRegister;

  return (
    <div className="flex min-h-full items-center justify-center bg-[#0b1120] px-4 py-10">
      <div className="w-full max-w-[400px]">
        <div className="mb-6 text-center">
          <div className="text-lg font-semibold tracking-wide text-slate-100">StockMaster</div>
          <div className="mt-1 text-xs text-slate-500">A股日线量化分析工作台</div>
        </div>

        <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-5 shadow-2xl shadow-black/40">
          {gateOff && (
            <div className="mb-4 rounded-md border border-amber-900/60 bg-amber-950/40 px-3 py-2 text-xs leading-relaxed text-amber-300">
              服务器未开启登录校验（SM_AUTH_ENABLED=false），可直接进入工作台。
              登录后仍可使用账号相关功能。
            </div>
          )}

          {showModeTabs && (
            <div className="mb-4 grid grid-cols-2 gap-1 rounded-lg bg-slate-950/70 p-1">
              {(["login", "register"] as Mode[]).map((item) => (
                <button
                  key={item}
                  type="button"
                  onClick={() => {
                    setMode(item);
                    setError("");
                  }}
                  className={`rounded-md py-1.5 text-sm transition ${
                    mode === item ? "bg-sky-600 text-white" : "text-slate-400 hover:text-slate-200"
                  }`}
                >
                  {item === "login" ? "登录" : "注册"}
                </button>
              ))}
            </div>
          )}

          <form className="space-y-3" onSubmit={submit}>
            <div>
              <label className="label" htmlFor="username">
                {mode === "login" ? "用户名 / 邮箱" : "用户名"}
              </label>
              <input
                id="username"
                className="input w-full"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
                required
                minLength={3}
                maxLength={64}
                placeholder="字母或数字开头，3-64 位"
              />
            </div>

            {mode === "register" && (
              <>
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
                  />
                </div>
                <div>
                  <label className="label" htmlFor="email">
                    邮箱（可选，用于找回）
                  </label>
                  <input
                    id="email"
                    type="email"
                    className="input w-full"
                    value={email}
                    onChange={(e) => setEmail(e.target.value)}
                    maxLength={255}
                  />
                </div>
              </>
            )}

            <div>
              <label className="label" htmlFor="password">
                密码
              </label>
              <input
                id="password"
                type="password"
                className="input w-full"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                required
                minLength={mode === "register" ? 8 : 1}
                maxLength={128}
                placeholder={mode === "register" ? "至少 8 位，含字母和数字" : ""}
              />
            </div>

            {error && (
              <div className="rounded-md border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs text-red-300">
                {error}
              </div>
            )}

            <button type="submit" className="btn btn-primary w-full py-2" disabled={busy}>
              {busy ? "处理中…" : mode === "login" ? "登录" : "注册并登录"}
            </button>
          </form>

          {wechatEnabled && (
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
              {config?.wechat_mode === "mock" && (
                <p className="mt-2 text-center text-[11px] text-amber-500/80">
                  当前为模拟微信授权（SM_WECHAT_LOGIN_MODE=mock），不会访问真实微信
                </p>
              )}
            </div>
          )}

          {!gateOff && config && !config.has_users && (
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
