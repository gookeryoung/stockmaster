/** 管理员「全部工作区」总览：只读，加前端搜索/排序，以及“以只读方式查看”入口。
 *
 * 数据来自 `GET /workspaces?all=true`（非管理员会静默退回本人列表，但这里只在
 * 管理员界面渲染）。[查看] 只切换数据上下文，不授予成员身份；非成员工作区会进入
 * 管理员查看模式（transient），全站只读，可从顶部工作区条「退出查看」。
 */

import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { formatDateTime } from "../../format";
import { useWorkspace } from "../../workspace";
import type { Workspace } from "../../types";

type SortKey = "default" | "members" | "data";

interface Props {
  workspaces: Workspace[] | undefined;
  loading: boolean;
  error: boolean;
  onRetry: () => void;
}

export default function AdminOverviewSection({ workspaces, loading, error, onRetry }: Props) {
  const navigate = useNavigate();
  const list = useWorkspace((state) => state.list);
  const currentId = useWorkspace((state) => state.currentId);
  const transient = useWorkspace((state) => state.transient);
  const select = useWorkspace((state) => state.select);
  const selectTransient = useWorkspace((state) => state.selectTransient);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortKey>("default");

  // “我参与的工作区”不包含 transient 那一项。
  const memberIds = useMemo(
    () => new Set(list.filter((row) => row.id !== transient?.id).map((row) => row.id)),
    [list, transient],
  );

  const onView = (row: Workspace) => {
    if (memberIds.has(row.id)) select(row.id);
    else if (transient?.id !== row.id) selectTransient(row);
    navigate("/workspace");
  };

  const rows = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    const filtered = (workspaces ?? []).filter((row) => {
      if (!keyword) return true;
      return (
        row.name.toLowerCase().includes(keyword) ||
        (row.description ?? "").toLowerCase().includes(keyword)
      );
    });
    if (sort === "default") return filtered;
    const copy = [...filtered];
    copy.sort((a, b) => (sort === "members" ? b.member_count - a.member_count : b.data_total - a.data_total));
    return copy;
  }, [workspaces, search, sort]);

  const personalCount = rows.filter((row) => row.kind === "personal").length;

  return (
    <section id="all-workspaces" className="scroll-mt-16">
      <div className="card">
        <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
          <div>
            <h2 className="text-sm font-semibold text-slate-100">全部工作区（管理员）</h2>
            <p className="mt-1 text-xs text-slate-500">
              只读 · 含个人与共享工作区；管理员可读取任意工作区。[查看] 会以只读方式进入该工作区
              上下文（非成员时顶部会提示“管理员查看模式”），不改变你的成员身份。
            </p>
          </div>
          <span className="text-xs text-slate-500">
            共 {rows.length} 个（个人 {personalCount} / 共享 {rows.length - personalCount}）
          </span>
        </div>

        <div className="mb-2 flex flex-wrap items-end gap-2">
          <div className="min-w-[14rem] flex-1">
            <label className="label" htmlFor="admin-workspace-search">
              ⌕ 搜索名称或描述
            </label>
            <input
              id="admin-workspace-search"
              className="input w-full"
              placeholder="输入关键字过滤"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
          </div>
          <div>
            <label className="label" htmlFor="admin-workspace-sort">
              排序
            </label>
            <select
              id="admin-workspace-sort"
              className="input"
              value={sort}
              onChange={(event) => setSort(event.target.value as SortKey)}
            >
              <option value="default">默认（创建顺序）</option>
              <option value="members">成员数最多</option>
              <option value="data">数据量最多</option>
            </select>
          </div>
        </div>

        {loading && <p className="text-xs text-slate-500">加载中…</p>}
        {!loading && error && (
          <p className="text-xs text-amber-300">
            全部工作区暂不可用{" "}
            <button className="underline" type="button" onClick={onRetry}>
              重试
            </button>
          </p>
        )}
        {!loading && !error && (
          <div className="table-wrap">
            <table className="w-full">
              <thead>
                <tr className="border-b border-slate-800">
                  <th className="th">名称</th>
                  <th className="th">类型</th>
                  <th className="th">所有者 ID</th>
                  <th className="th text-right">成员</th>
                  <th className="th text-right">数据量</th>
                  <th className="th">创建时间</th>
                  <th className="th" />
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id} className="border-b border-slate-800/50 last:border-0">
                    <td className="td">
                      <span className="font-medium text-slate-100">{row.name}</span>
                      {row.description && <span className="ml-2 text-xs text-slate-500">{row.description}</span>}
                    </td>
                    <td className="td text-slate-400">{row.kind === "personal" ? "个人" : "共享"}</td>
                    <td className="td text-slate-500">{row.owner_id ? `#${row.owner_id}` : "—"}</td>
                    <td className="td text-right text-slate-400">{row.member_count}</td>
                    <td className="td text-right text-slate-300">{row.data_total} 条</td>
                    <td className="td text-xs text-slate-400">{formatDateTime(row.created_at)}</td>
                    <td className="td text-right">
                      <button
                        className="btn px-2 py-1 text-xs"
                        disabled={row.id === currentId}
                        title={
                          memberIds.has(row.id)
                            ? "切换到这个工作区"
                            : "以只读方式查看该工作区（管理员查看模式）"
                        }
                        onClick={() => onView(row)}
                      >
                        {row.id === currentId ? (transient?.id === row.id ? "查看中" : "当前") : "查看"}
                      </button>
                    </td>
                  </tr>
                ))}
                {rows.length === 0 && (
                  <tr>
                    <td className="td text-slate-500" colSpan={7}>
                      {search.trim() ? "没有匹配的工作区。" : "还没有任何工作区。"}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}
