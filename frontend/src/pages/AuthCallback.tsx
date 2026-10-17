import { useEffect, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api } from "../api";
import { useAuth } from "../auth";

/** Landing page of the WeChat OAuth round-trip.
 *
 * The backend never puts a real access token in the URL — it hands over a
 * single-use ticket, which is exchanged here for the actual session.
 */
export default function AuthCallback() {
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const applyToken = useAuth((state) => state.applyToken);
  const [error, setError] = useState("");
  const started = useRef(false);

  useEffect(() => {
    // React StrictMode replays effects; a ticket is single-use, so the exchange
    // must happen exactly once per page load.
    if (started.current) return;
    started.current = true;

    const ticket = params.get("ticket");
    const next = params.get("next") || "/";
    const bound = params.get("bound") === "1";

    if (!ticket) {
      setError("登录回调缺少 ticket 参数，请重新发起登录");
      return;
    }

    api
      .exchangeTicket(ticket)
      .then((result) => {
        applyToken(result.token, result.user);
        navigate(bound ? "/account?bound=1" : next, { replace: true });
      })
      .catch((err: Error) => setError(err.message));
  }, [params, navigate, applyToken]);

  return (
    <div className="flex min-h-full items-center justify-center px-4">
      <div className="w-full max-w-[380px] rounded-xl border border-slate-800 bg-slate-900/70 p-6 text-center">
        {error ? (
          <>
            <div className="text-sm font-medium text-red-400">登录失败</div>
            <p className="mt-2 text-xs leading-relaxed text-slate-400">{error}</p>
            <Link to="/login" className="btn btn-primary mt-4 inline-block">
              返回登录
            </Link>
          </>
        ) : (
          <>
            <div className="mx-auto h-6 w-6 animate-spin rounded-full border-2 border-slate-700 border-t-sky-500" />
            <div className="mt-4 text-sm text-slate-300">正在完成登录…</div>
          </>
        )}
      </div>
    </div>
  );
}
