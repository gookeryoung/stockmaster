import { NavLink, Route, Routes } from "react-router-dom";
import StatusBar from "./StatusBar";
import Dashboard from "./pages/Dashboard";
import Market from "./pages/Market";
import Analyze from "./pages/Analyze";
import Predict from "./pages/Predict";
import Backtest from "./pages/Backtest";
import Alerts from "./pages/Alerts";
import Settings from "./pages/Settings";

const NAV = [
  { to: "/", label: "总览" },
  { to: "/market", label: "行情" },
  { to: "/analyze", label: "分析" },
  { to: "/predict", label: "预测" },
  { to: "/backtest", label: "回测" },
  { to: "/alerts", label: "预警" },
  { to: "/settings", label: "设置" },
];

export default function App() {
  return (
    <div className="flex min-h-full flex-col">
      <header className="border-b border-slate-800 bg-slate-950">
        <div className="flex items-center gap-6 px-4 py-2.5">
          <div className="text-base font-semibold tracking-wide text-slate-100">
            StockMaster
            <span className="ml-2 text-xs font-normal text-slate-500">
              A股日线量化分析工作台
            </span>
          </div>
          <nav className="flex flex-wrap gap-1">
            {NAV.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                className={({ isActive }) =>
                  `rounded-md px-3 py-1.5 text-sm transition ${
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
        </div>
        <StatusBar />
      </header>

      <main className="flex-1 overflow-auto p-4">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/market" element={<Market />} />
          <Route path="/analyze" element={<Analyze />} />
          <Route path="/predict" element={<Predict />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="/alerts" element={<Alerts />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>

      <footer className="border-t border-slate-800 px-4 py-2 text-xs text-slate-500">
        本工作台为量化研究工具，不构成投资建议。历史回测与模型预测均不代表未来收益。
      </footer>
    </div>
  );
}
