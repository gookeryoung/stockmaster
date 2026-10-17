import { useEffect } from "react";
import { Link, NavLink, Navigate, Route, Routes, useLocation } from "react-router-dom";
import StatusBar from "./StatusBar";
import { canManageUsers, requiresLogin, useAuth } from "./auth";
import { ROLE_LABEL, useWorkspace } from "./workspace";
import Dashboard from "./pages/Dashboard";
import Market from "./pages/Market";
import Analyze from "./pages/Analyze";
import Predict from "./pages/Predict";
import Backtest from "./pages/Backtest";
import Alerts from "./pages/Alerts";
import StrategyConfigs from "./pages/StrategyConfigs";
import Settings from "./pages/Settings";
import Login from "./pages/Login";
import AuthCallback from "./pages/AuthCallback";
import Account from "./pages/Account";
import Users from "./pages/Users";
import WorkspacePage from "./pages/Workspace";

export default function App() {
  const ready = useAuth((state) => state.ready);
  const init = useAuth((state) => state.init);

  useEffect(() => {
    void init();
  }, [init]);

  if (!ready) {
    return <div className="flex min-h-full items-center justify-center text-sm text-slate-500">正在加载…</div>;
  }

  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/auth/callback" element={<AuthCallback />} />
      <Route
        path="/*"
        element={
          <SessionGate>
            <Layout />
          </SessionGate>
        }
      />
    </Routes>
  );
}

/** Blocks the workbench until a session exists (only when the gate is on). */
function SessionGate({ children }: { children: React.ReactNode }) {
  const config = useAuth((state) => state.config);
  const user = useAuth((state) => state.user);
  const location = useLocation();

  if (requiresLogin(config, user)) {
    const next = `${location.pathname}${location.search}`;
    return <Navigate to={`/login?next=${encodeURIComponent(next)}`} replace />;
  }
  return <>{children}</>;
}

function Layout() {
  const config = useAuth((state) => state.config);
  const user = useAuth((state) => state.user);
  const showUsers = canManageUsers(config, user);
  const workspacesEnabled = Boolean(config?.workspace_enabled && user);
  const currentId = useWorkspace((state) => state.currentId);
  const refreshWorkspaces = useWorkspace((state) => state.refresh);
  const resetWorkspaces = useWorkspace((state) => state.reset);

  // 登录态变化时重新拉取「我的工作区」。换账号必须重置，否则会带着上一个人的
  // 工作区 id 发请求（后端只会 404，但界面会一直在报错）。
  const userId = user?.id ?? null;
  useEffect(() => {
    if (!workspacesEnabled || userId === null) {
      resetWorkspaces();
      return;
    }
    void refreshWorkspaces();
  }, [workspacesEnabled, userId, refreshWorkspaces, resetWorkspaces]);

  const nav = [
    { to: "/", label: "总览" },
    { to: "/market", label: "行情" },
    { to: "/analyze", label: "分析" },
    { to: "/strategy", label: "策略" },
    { to: "/predict", label: "预测" },
    { to: "/backtest", label: "回测" },
    { to: "/alerts", label: "预警" },
    { to: "/settings", label: "设置" },
    ...(workspacesEnabled ? [{ to: "/workspace", label: "工作区" }] : []),
    ...(showUsers ? [{ to: "/users", label: "用户" }] : []),
  ];

  return (
    <div className="flex min-h-full flex-col">
      <header className="sticky top-0 z-20 border-b border-slate-800 bg-slate-950/95 backdrop-blur">
        <div className="flex flex-col gap-1.5 px-3 py-2 sm:flex-row sm:items-center sm:gap-6 sm:px-4 sm:py-2.5">
          <div className="flex shrink-0 items-center justify-between gap-3">
            <div className="text-sm font-semibold tracking-wide text-slate-100 sm:text-base">
              StockMaster
              <span className="ml-2 hidden text-xs font-normal text-slate-500 sm:inline">
                A股日线量化分析工作台
              </span>
            </div>
            <div className="sm:hidden">
              <UserMenu />
            </div>
          </div>
          <nav className="-mx-3 flex gap-1 overflow-x-auto px-3 pb-0.5 sm:mx-0 sm:flex-wrap sm:px-0 sm:pb-0">
            {nav.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                className={({ isActive }) =>
                  `shrink-0 rounded-md px-3 py-1.5 text-sm transition ${
                    isActive
                      ? "bg-sky-600 text-white"
                      : "text-slate-300 hover:bg-slate-800 hover:text-white"
                  }`
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto hidden sm:block">
            <UserMenu />
          </div>
        </div>
        <WorkspaceBar />
        <StatusBar />
      </header>

      <main className="flex-1 overflow-auto p-3 sm:p-4">
        {/* 换工作区 = 换数据源：用 key 强制整棵路由子树重挂载，所有 useQuery 自动重取。 */}
        <Routes key={currentId ?? "local"}>
          <Route path="/" element={<Dashboard />} />
          <Route path="/market" element={<Market />} />
          <Route path="/analyze" element={<Analyze />} />
          <Route path="/predict" element={<Predict />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="/alerts" element={<Alerts />} />
          <Route path="/strategy" element={<StrategyConfigs />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/workspace" element={<WorkspacePage />} />
          <Route path="/account" element={<Account />} />
          {showUsers && <Route path="/users" element={<Users />} />}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>

      <footer className="border-t border-slate-800 px-3 py-2 text-[11px] text-slate-500 sm:px-4 sm:text-xs">
        本工作台为量化研究工具，不构成投资建议。历史回测与模型预测均不代表未来收益。
      </footer>
    </div>
  );
}

/** 工作区切换器：只在数据按工作区隔离、且已登录时出现。 */
function WorkspaceBar() {
  const config = useAuth((state) => state.config);
  const user = useAuth((state) => state.user);
  const list = useWorkspace((state) => state.list);
  const currentId = useWorkspace((state) => state.currentId);
  const select = useWorkspace((state) => state.select);
  const loading = useWorkspace((state) => state.loading);

  if (!config?.workspace_enabled || !user) return null;

  const current = list.find((row) => row.id === currentId) ?? null;
  const tone =
    current?.role === "viewer"
      ? "bg-amber-900/50 text-amber-300"
      : current?.role === "owner"
        ? "bg-emerald-900/50 text-emerald-300"
        : "bg-sky-900/50 text-sky-300";

  return (
    <div className="flex items-center gap-2 border-t border-slate-800/60 px-3 py-1.5 text-xs sm:px-4">
      <span className="shrink-0 text-slate-500">工作区</span>
      <select
        className="input max-w-[12rem] py-1 text-xs"
        value={currentId ?? ""}
        onChange={(event) => select(Number(event.target.value))}
        disabled={loading || list.length === 0}
      >
        {list.length === 0 && <option value="">（加载中）</option>}
        {list.map((row) => (
          <option key={row.id} value={row.id}>
            {row.name}
            {row.kind === "personal" ? "（个人）" : ""}
          </option>
        ))}
      </select>
      {current && <span className={`badge shrink-0 ${tone}`}>{ROLE_LABEL[current.role] ?? current.role}</span>}
      {current?.role === "viewer" && <span className="shrink-0 text-amber-400/90">只读</span>}
      <Link to="/workspace" className="ml-auto shrink-0 text-sky-400 hover:text-sky-300">
        成员与设置
      </Link>
    </div>
  );
}

/** Account entry point: session menu when logged in, otherwise a login link. */
function UserMenu() {
  const { user, config, logout } = useAuth();
  const showUsers = canManageUsers(config, user);

  if (!user) {
    if (!config?.auth_enabled) {
      return (
        <Link to="/login" className="btn px-2 py-1 text-xs text-slate-400">
          账号入口
        </Link>
      );
    }
    return (
      <Link to="/login" className="btn btn-primary px-2 py-1 text-xs">
        登录
      </Link>
    );
  }

  return (
    <details className="group relative">
      <summary className="flex cursor-pointer list-none items-center gap-2 rounded-md border border-slate-800 px-2 py-1 text-xs text-slate-300 hover:border-slate-600">
        <span className="flex h-5 w-5 items-center justify-center rounded-full bg-sky-900/60 text-[10px] text-sky-300">
          {(user.nickname || user.username).slice(0, 1).toUpperCase()}
        </span>
        <span className="max-w-[8rem] truncate">{user.nickname || user.username}</span>
        <span className="text-slate-600">▾</span>
      </summary>
      <div className="absolute right-0 z-30 mt-1 w-40 overflow-hidden rounded-md border border-slate-800 bg-slate-900 py-1 shadow-xl">
        <div className="px-3 pb-1 pt-0.5 text-[11px] text-slate-500">
          {user.role === "admin" ? "管理员" : "普通用户"}
        </div>
        <Link to="/account" className="block px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800">
          账号设置
        </Link>
        {showUsers && (
          <Link to="/users" className="block px-3 py-1.5 text-xs text-slate-300 hover:bg-slate-800">
            用户管理
          </Link>
        )}
        <button
          className="block w-full px-3 py-1.5 text-left text-xs text-slate-300 hover:bg-slate-800"
          onClick={async () => {
            await logout();
            window.location.assign("/login");
          }}
        >
          退出登录
        </button>
      </div>
    </details>
  );
}
