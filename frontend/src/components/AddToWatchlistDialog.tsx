/**
 * 选股结果 → 自选清单的通用弹层（§4.8 / §5.2.5）。
 *
 * 只负责「把一批代码导入某个清单」这一件事：目标清单（已有或新建）、是否跳过已存在、
 * 一次 POST /watchlist/import、结果汇总与跳转。调用方（分析 / 行情页）决定
 * 是否可写（viewer 由页面禁用或隐藏入口），组件本身不做权限判断。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { api } from "../api";
import type { WatchlistImportResult } from "../types";

const STORAGE_KEY = "sm.watchlist.list";
const DEFAULT_LIST = "default";
const MAX_LIST_NAME = 64;

interface AddToWatchlistDialogProps {
  open: boolean;
  codes: string[];
  /** 代码 → 名称，用于弹层内的预览（可选）。 */
  names?: Record<string, string>;
  onClose: () => void;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export default function AddToWatchlistDialog({
  open,
  codes,
  names,
  onClose,
}: AddToWatchlistDialogProps) {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const nameInputRef = useRef<HTMLInputElement | null>(null);
  const viewButtonRef = useRef<HTMLButtonElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);

  const [target, setTarget] = useState<"existing" | "new">("existing");
  const [selectedList, setSelectedList] = useState(DEFAULT_LIST);
  const [newListName, setNewListName] = useState("");
  const [skipExisting, setSkipExisting] = useState(true);
  const [error, setError] = useState("");
  const [result, setResult] = useState<WatchlistImportResult | null>(null);

  const uniqueCodes = useMemo(
    () => Array.from(new Set(codes.map((code) => code.trim()).filter(Boolean))),
    [codes],
  );
  const total = uniqueCodes.length;

  const lists = useQuery({
    queryKey: ["watchlist-lists"],
    queryFn: () => api.watchlistLists(),
    enabled: open,
  });
  const listOptions = lists.data ?? [];

  // 每次打开都从干净状态开始，关闭后重新打开不会留在上一次结果页。
  useEffect(() => {
    if (!open) return;
    setTarget("existing");
    setSelectedList(DEFAULT_LIST);
    setNewListName("");
    setSkipExisting(true);
    setError("");
    setResult(null);
  }, [open]);

  // 清单加载完成后校正默认选项：default 固定优先，没有则选第一个；空清单直接新建。
  useEffect(() => {
    if (!open || !lists.data) return;
    if (lists.data.length === 0) {
      setTarget("new");
      return;
    }
    if (!lists.data.some((item) => item.list_name === DEFAULT_LIST)) {
      setSelectedList(lists.data[0].list_name);
    }
  }, [open, lists.data]);

  // 清单接口不可用时（例如后端尚未上线）降级为「新建清单」，不让弹层卡死。
  useEffect(() => {
    if (!open || !lists.isError) return;
    setTarget("new");
  }, [open, lists.isError]);

  // 焦点管理：打开时把焦点移入弹层，关闭时还给触发按钮。
  useEffect(() => {
    if (!open) return;
    previousFocusRef.current = document.activeElement as HTMLElement | null;
    const timer = window.setTimeout(() => dialogRef.current?.focus(), 0);
    return () => {
      window.clearTimeout(timer);
      previousFocusRef.current?.focus?.();
    };
  }, [open]);

  useEffect(() => {
    if (open && target === "new") nameInputRef.current?.focus();
  }, [open, target]);

  // 提交成功后把焦点送到「查看清单 →」，键盘用户不必再 Tab 一圈。
  useEffect(() => {
    if (result) requestAnimationFrame(() => viewButtonRef.current?.focus());
  }, [result]);

  // Esc 关闭 + Tab 焦点循环，保证 role="dialog" 的键盘语义完整。
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusables = Array.from(
        dialogRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => element.offsetParent !== null);
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onClose]);

  const importList = useMutation({
    mutationFn: (listName: string) =>
      api.importWatchlist({
        list_name: listName,
        codes: uniqueCodes,
        create_list: true,
        skip_existing: skipExisting,
      }),
    onSuccess: (data) => {
      setResult(data);
      setError("");
      void queryClient.invalidateQueries({ queryKey: ["watchlist"] });
      void queryClient.invalidateQueries({ queryKey: ["watchlist-lists"] });
    },
    onError: (err) => setError(errorText(err)),
  });

  if (!open || total === 0) return null;

  const targetListName = target === "existing" ? selectedList : newListName.trim();

  const submit = () => {
    setError("");
    if (!targetListName) {
      setError("请输入新清单名称");
      nameInputRef.current?.focus();
      return;
    }
    if (targetListName.length > MAX_LIST_NAME) {
      setError(`清单名最多 ${MAX_LIST_NAME} 个字符`);
      nameInputRef.current?.focus();
      return;
    }
    importList.mutate(targetListName);
  };

  const openWatchlist = () => {
    const listName = result?.list_name || targetListName;
    try {
      window.localStorage.setItem(STORAGE_KEY, listName);
    } catch {
      // 私密模式 / 禁用存储：跳转仍然可用，自选页回退到 default。
    }
    onClose();
    navigate("/watchlist");
  };

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 px-4 py-6"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !importList.isPending) onClose();
      }}
    >
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="add-to-watchlist-title"
        className="w-full max-w-[440px] rounded-lg border border-slate-800 bg-slate-900 p-4 shadow-2xl shadow-black/40 outline-none"
      >
        <div className="flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <div id="add-to-watchlist-title" className="text-sm font-medium text-slate-200">
              {result ? "导入完成" : "加入自选"}
            </div>
            <p className="mt-1 text-[11px] text-slate-500">
              {result ? `目标清单「${result.list_name}」` : `共 ${total} 只标的`}
            </p>
          </div>
          <button
            type="button"
            className="btn px-2 py-1 text-xs"
            onClick={onClose}
            disabled={importList.isPending}
          >
            关闭
          </button>
        </div>

        {result ? (
          <div className="mt-3 space-y-3">
            <div className="flex flex-wrap gap-2 text-xs">
              <span className="badge bg-emerald-900/50 text-emerald-300">新增 {result.added}</span>
              <span className="badge bg-sky-900/50 text-sky-300">更新 {result.updated}</span>
              <span className="badge bg-slate-800 text-slate-300">跳过 {result.skipped}</span>
              <span
                className={`badge ${
                  result.invalid.length > 0
                    ? "bg-amber-900/50 text-amber-300"
                    : "bg-slate-800 text-slate-300"
                }`}
              >
                非法 {result.invalid.length}
              </span>
            </div>

            <p className="text-[11px] leading-relaxed text-slate-500">
              {result.list_created ? `清单「${result.list_name}」已创建，共 ` : "当前清单共 "}
              {result.total_items} 条。
              {result.quota
                ? `（工作区自选上限 ${result.quota.limit}，已用 ${result.quota.used}）`
                : ""}
            </p>

            {result.invalid.length > 0 && (
              <div className="max-h-32 overflow-auto rounded-md border border-slate-800 bg-slate-950/60 p-2">
                <div className="mb-1 text-[11px] text-slate-400">未加入的代码</div>
                {result.invalid.map((item) => (
                  <p key={`${item.index}-${item.code}`} className="text-[11px] text-amber-300">
                    {item.code || `第 ${item.index + 1} 项`}：{item.message || item.reason}
                  </p>
                ))}
              </div>
            )}

            <div className="flex justify-end gap-2">
              <button type="button" className="btn" onClick={onClose}>
                继续挑选
              </button>
              <button
                ref={viewButtonRef}
                type="button"
                className="btn btn-primary"
                onClick={openWatchlist}
              >
                查看清单 →
              </button>
            </div>
          </div>
        ) : (
          <form
            className="mt-3 space-y-3"
            onSubmit={(event) => {
              event.preventDefault();
              submit();
            }}
          >
            <div className="flex flex-wrap gap-1">
              {uniqueCodes.slice(0, 5).map((code) => (
                <span key={code} className="badge bg-slate-800 text-slate-300">
                  {code}
                  {names?.[code] ? ` ${names[code]}` : ""}
                </span>
              ))}
              {total > 5 && (
                <span className="badge bg-slate-800 text-slate-400">另 {total - 5} 只</span>
              )}
            </div>

            <div className="space-y-2" role="radiogroup" aria-label="目标清单">
              <label className="flex items-center gap-2 text-sm text-slate-200" htmlFor="target-existing">
                <input
                  id="target-existing"
                  type="radio"
                  name="watchlist-target"
                  checked={target === "existing"}
                  onChange={() => setTarget("existing")}
                  disabled={listOptions.length === 0}
                />
                已有清单
              </label>
              {target === "existing" && (
                <select
                  className="input w-full"
                  value={selectedList}
                  onChange={(event) => setSelectedList(event.target.value)}
                  disabled={listOptions.length === 0}
                  aria-label="选择已有清单"
                >
                  {listOptions.length === 0 && <option value="">（暂无清单）</option>}
                  {listOptions.map((item) => (
                    <option key={item.list_name} value={item.list_name}>
                      {item.list_name}（{item.count} 只）
                    </option>
                  ))}
                </select>
              )}

              <label className="flex items-center gap-2 text-sm text-slate-200" htmlFor="target-new">
                <input
                  id="target-new"
                  type="radio"
                  name="watchlist-target"
                  checked={target === "new"}
                  onChange={() => setTarget("new")}
                />
                新建清单
              </label>
              {target === "new" && (
                <input
                  ref={nameInputRef}
                  className="input w-full"
                  value={newListName}
                  maxLength={MAX_LIST_NAME}
                  placeholder="新清单名，如 观察池-1017"
                  onChange={(event) => setNewListName(event.target.value)}
                  aria-label="新清单名称"
                />
              )}
            </div>

            {lists.isFetching && (
              <p className="text-[11px] text-slate-500">正在读取已有清单…</p>
            )}
            {lists.isError && (
              <p className="text-[11px] leading-relaxed text-amber-300">
                已有清单读取失败：{errorText(lists.error)}。可改为新建清单。
              </p>
            )}

            <label className="flex items-start gap-2 text-xs text-slate-300" htmlFor="skip-existing">
              <input
                id="skip-existing"
                type="checkbox"
                className="mt-0.5"
                checked={skipExisting}
                onChange={(event) => setSkipExisting(event.target.checked)}
              />
              <span>
                已存在的股票跳过
                <span className="mt-0.5 block text-[11px] text-slate-500">
                  取消勾选时已存在的条目计入「更新」，不覆盖原有备注。
                </span>
              </span>
            </label>

            {error && (
              <p
                role="alert"
                className="rounded-md border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs leading-relaxed text-red-300"
              >
                {error}
              </p>
            )}

            <div className="flex justify-end gap-2">
              <button type="button" className="btn" onClick={onClose} disabled={importList.isPending}>
                取消
              </button>
              <button
                type="submit"
                className="btn btn-primary"
                disabled={importList.isPending || !targetListName}
              >
                {importList.isPending ? "加入中…" : `加入 ${total} 只`}
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
