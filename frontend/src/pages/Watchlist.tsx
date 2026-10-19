/** 自选页（P1/P2）：多清单 + 批量行情/评分 + CSV 导入导出。
 *
 * 设计依据 `docs/UI_WATCHLIST.md` §4.1–§4.9。页面只负责交互与派生状态：
 * - 清单是 `watchlist_items.list_name` 的派生物，空清单先存在本地草稿里，首次加入才落库；
 * - 行情/评分分别走一次批量接口（`/market/quotes`、`/analysis/factors`），不再逐只请求；
 * - 批量删除、导入、重命名/删除清单都走后端 P1 端点，前端不做“假批量”；
 * - CSV 导入导出全部在前端完成，不新增依赖，也不新增导出端点。
 *
 * 当前清单名沿用 `localStorage["sm.watchlist.list"]`，与 `AddToWatchlistDialog` 的约定一致。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { ApiError, api } from "../api";
import ConfirmDialog from "../components/ConfirmDialog";
import { changeColor, formatCompact, formatNumber, formatPct, todayISO } from "../format";
import { canWriteWorkspace, useCurrentWorkspace } from "../workspace";
import { useViewState } from "../view";
import type { QuoteRow, WatchlistImportResult, WatchlistItem, WatchlistList } from "../types";

const STORAGE_KEY = "sm.watchlist.list";
const DRAFTS_KEY = "sm.watchlist.drafts";
const AUTO_REFRESH_KEY = "sm.watchlist.auto-refresh";
const DEFAULT_LIST = "default";
const MAX_LIST_NAME = 64;
const MAX_IMPORT_ROWS = 2000;
const REFETCH_INTERVAL_MS = 60_000;

type SortField = "code" | "name" | "pct_chg";

interface SortState {
  field: SortField;
  direction: "asc" | "desc";
}

interface Notice {
  kind: "ok" | "warn" | "error";
  text: string;
}

interface CsvRecord {
  fields: string[];
  line: number;
}

interface CsvRow {
  line: number;
  code: string;
  name: string;
  note: string;
}

function readStoredList(): string {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return stored && stored.trim() ? stored : DEFAULT_LIST;
  } catch {
    return DEFAULT_LIST;
  }
}

/** 本地草稿清单：为空清单只存在本地，避免切走后“新建的清单”凭空消失。 */
function readDrafts(): string[] {
  try {
    const raw = window.localStorage.getItem(DRAFTS_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter((name): name is string => typeof name === "string") : [];
  } catch {
    return [];
  }
}

function readAutoRefresh(): boolean {
  try {
    return window.localStorage.getItem(AUTO_REFRESH_KEY) === "1";
  } catch {
    return false;
  }
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function formatScore(value: number | null | undefined): string {
  return value === null || value === undefined || Number.isNaN(value) ? "--" : value.toFixed(2);
}

/** CSV 单元格：导出时防公式注入，同时处理引号/逗号/换行。 */
function csvCell(value: string): string {
  let text = value ?? "";
  if (/^[=+\-@]/.test(text)) text = `'${text}`;
  if (/[",\r\n]/.test(text)) return `"${text.replace(/"/g, '""')}"`;
  return text;
}

function safeFileName(name: string): string {
  return name.replace(/[\\/:*?"<>|]/g, "_") || DEFAULT_LIST;
}

/**
 * 解析 CSV（支持 CRLF/LF、双引号转义、引号内换行）。
 * 返回记录的起始行号，保证错误提示能对回原始文件。
 */
function parseCsvRecords(text: string): CsvRecord[] {
  const source = text.replace(/^\uFEFF/, "");
  const records: CsvRecord[] = [];
  let fields: string[] = [];
  let field = "";
  let inQuotes = false;
  let line = 1;
  let recordLine = 1;

  for (let index = 0; index < source.length; index += 1) {
    const char = source[index];
    if (inQuotes) {
      if (char === '"') {
        if (source[index + 1] === '"') {
          field += '"';
          index += 1;
        } else {
          inQuotes = false;
        }
      } else {
        if (char === "\n") line += 1;
        field += char;
      }
      continue;
    }
    if (char === '"') {
      inQuotes = true;
    } else if (char === ",") {
      fields.push(field);
      field = "";
    } else if (char === "\r") {
      // CRLF 的 \r 直接跳过；单独的 \r 不作为换行（宁可多解析一行也不吞内容）。
    } else if (char === "\n") {
      fields.push(field);
      records.push({ fields, line: recordLine });
      fields = [];
      field = "";
      line += 1;
      recordLine = line;
    } else {
      field += char;
    }
  }
  if (field.length > 0 || fields.length > 0) {
    fields.push(field);
    records.push({ fields, line: recordLine });
  }
  return records;
}

export default function Watchlist() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const workspace = useCurrentWorkspace();
  const readOnly = !canWriteWorkspace(workspace);

  const [listName, setListName] = useState<string>(readStoredList);
  const [drafts, setDrafts] = useState<string[]>(readDrafts);
  const [search, setSearch] = useState("");
  const [sort, setSort] = useState<SortState>({ field: "code", direction: "asc" });
  const [selected, setSelected] = useState<Set<number>>(new Set<number>());
  const [notice, setNotice] = useState<Notice | null>(null);
  const [batchBusy, setBatchBusy] = useState(false);
  const [removingId, setRemovingId] = useState<number | null>(null);
  const [autoRefresh, setAutoRefresh] = useState<boolean>(readAutoRefresh);

  // 统一确认弹层：单条移出 / 批量删除 / 删除清单
  const [removeTarget, setRemoveTarget] = useState<WatchlistItem | null>(null);
  const [batchConfirmOpen, setBatchConfirmOpen] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);

  // 添加自选
  const [addQuery, setAddQuery] = useState("");
  const [addOpen, setAddOpen] = useState(false);
  const [addBusy, setAddBusy] = useState(false);
  const addInputRef = useRef<HTMLInputElement | null>(null);

  // 备注内联编辑
  const [editingId, setEditingId] = useState<number | null>(null);
  const [noteDraft, setNoteDraft] = useState("");
  const [savingNote, setSavingNote] = useState(false);
  const [noteError, setNoteError] = useState<string | null>(null);
  const cancelEditRef = useRef(false);

  // 新建/重命名清单
  const [listDialogMode, setListDialogMode] = useState<"new" | "rename" | null>(null);
  const [listNameDraft, setListNameDraft] = useState("");
  const [listFormError, setListFormError] = useState("");
  const [listBusy, setListBusy] = useState(false);

  // CSV 导入
  const [importOpen, setImportOpen] = useState(false);
  const [importFileName, setImportFileName] = useState("");
  const [importRows, setImportRows] = useState<CsvRow[]>([]);
  const [importError, setImportError] = useState("");
  const [importBusy, setImportBusy] = useState(false);
  const [importResult, setImportResult] = useState<WatchlistImportResult | null>(null);
  const [skipExisting, setSkipExisting] = useState(true);
  const [importTargetMode, setImportTargetMode] = useState<"existing" | "new">("existing");
  const [importTargetList, setImportTargetList] = useState<string>(DEFAULT_LIST);
  const [importTargetNew, setImportTargetNew] = useState("");
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const selectAllRef = useRef<HTMLInputElement | null>(null);

  // --- 清单与条目 ---------------------------------------------------------- //
  const listsQuery = useQuery({
    queryKey: ["watchlist-lists"],
    queryFn: () => api.watchlistLists(),
    refetchInterval: autoRefresh ? REFETCH_INTERVAL_MS : false,
  });
  const itemsQuery = useQuery({
    queryKey: ["watchlist", listName],
    queryFn: () => api.watchlist(listName),
    refetchInterval: autoRefresh ? REFETCH_INTERVAL_MS : false,
  });

  const serverLists = listsQuery.data ?? [];
  const items = itemsQuery.data ?? [];

  const listOptions = useMemo(() => {
    const names = new Set(serverLists.map((row) => row.list_name));
    const merged: WatchlistList[] = [
      ...serverLists,
      ...drafts
        .filter((name) => !names.has(name))
        .map((name) => ({ list_name: name, count: 0, last_added_at: null })),
    ];
    if (!merged.some((row) => row.list_name === listName)) {
      merged.push({ list_name: listName, count: 0, last_added_at: null });
    }
    return merged;
  }, [serverLists, drafts, listName]);

  const isDraftList = !serverLists.some((row) => row.list_name === listName);
  const currentListCount = items.length;

  const codes = useMemo(() => items.map((item) => item.code), [items]);
  const codesKey = codes.join(",");

  const quotesQuery = useQuery({
    queryKey: ["watchlist-quotes", codesKey],
    queryFn: () => api.quotes(codes),
    enabled: codes.length > 0,
    staleTime: 60_000,
    retry: false,
    refetchInterval: autoRefresh ? REFETCH_INTERVAL_MS : false,
  });
  const scoresQuery = useQuery({
    queryKey: ["watchlist-scores", codesKey],
    queryFn: () => api.factorScores(codes),
    enabled: codes.length > 0,
    staleTime: 5 * 60_000,
    retry: false,
    refetchInterval: autoRefresh ? REFETCH_INTERVAL_MS : false,
  });

  // 重命名/删除前检查引用：失败只提示，不阻塞操作。
  const referenceChecksEnabled = listDialogMode === "rename" || deleteDialogOpen;
  const alertRulesQuery = useQuery({
    queryKey: ["watchlist-ref-alerts"],
    queryFn: () => api.alertRules(),
    enabled: referenceChecksEnabled,
    staleTime: 0,
  });
  const alertRefs = useMemo(
    () =>
      (alertRulesQuery.data ?? []).filter(
        (rule) => rule.scope_type === "watchlist" && rule.scope_value === listName,
      ),
    [alertRulesQuery.data, listName],
  );
  const refCheckLoading = referenceChecksEnabled && alertRulesQuery.isFetching;
  const refCheckFailed = alertRulesQuery.isError;

  const quoteMap = useMemo(() => {
    const map = new Map<string, QuoteRow>();
    (quotesQuery.data?.items ?? []).forEach((row) => map.set(row.code, row));
    return map;
  }, [quotesQuery.data]);

  const scoreMap = useMemo(() => {
    const map = new Map<string, { score: number | null; trade_date: string | null }>();
    (scoresQuery.data?.items ?? []).forEach((row) => map.set(row.code, row));
    return map;
  }, [scoresQuery.data]);

  const existingCodes = useMemo(() => new Set(items.map((item) => item.code)), [items]);

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, listName);
    } catch {
      // 私密模式/禁用存储时仅影响下次打开时的默认清单，不影响本次操作。
    }
  }, [listName]);

  useEffect(() => {
    try {
      window.localStorage.setItem(DRAFTS_KEY, JSON.stringify(drafts));
    } catch {
      // 同上：存储不可用时草稿只在当前会话内有效。
    }
  }, [drafts]);

  useEffect(() => {
    try {
      window.localStorage.setItem(AUTO_REFRESH_KEY, autoRefresh ? "1" : "0");
    } catch {
      // 存储不可用时自动刷新只在当前会话内生效。
    }
  }, [autoRefresh]);

  // 切换清单时重置所有“对某一行”的临时状态，避免把旧清单的编辑态带过去。
  useEffect(() => {
    setSelected(new Set<number>());
    setAddQuery("");
    setAddOpen(false);
    setEditingId(null);
    setNoteError(null);
    setSearch("");
  }, [listName]);

  useEffect(() => {
    setSelected(new Set<number>());
  }, [search]);

  // 服务端清单是唯一真相：草稿清单一旦落库就从本地草稿移除；当前清单始终可见。
  useEffect(() => {
    if (!listsQuery.data) return;
    const names = new Set(listsQuery.data.map((row) => row.list_name));
    setDrafts((current) => {
      const next = current.filter((name) => !names.has(name));
      if (!names.has(listName) && !next.includes(listName)) next.push(listName);
      const unchanged = next.length === current.length && next.every((name, index) => name === current[index]);
      return unchanged ? current : next;
    });
  }, [listsQuery.data, listName]);

  useEffect(() => {
    const valid = new Set(items.map((item) => item.id));
    setSelected((current) => {
      let changed = false;
      const next = new Set<number>();
      current.forEach((id) => {
        if (valid.has(id)) next.add(id);
        else changed = true;
      });
      return changed ? next : current;
    });
  }, [items]);

  const rows = useMemo(() => {
    const keyword = search.trim().toLowerCase();
    const filtered = items.filter((item) => {
      if (!keyword) return true;
      return (
        item.code.toLowerCase().includes(keyword) ||
        item.name.toLowerCase().includes(keyword) ||
        item.note.toLowerCase().includes(keyword)
      );
    });
    const direction = sort.direction === "asc" ? 1 : -1;
    return [...filtered].sort((a, b) => {
      if (sort.field === "name") {
        return a.name.localeCompare(b.name, "zh-CN") * direction || a.code.localeCompare(b.code);
      }
      if (sort.field === "pct_chg") {
        const av = quoteMap.get(a.code)?.pct_chg ?? null;
        const bv = quoteMap.get(b.code)?.pct_chg ?? null;
        if (av === null && bv === null) return a.code.localeCompare(b.code);
        if (av === null) return 1;
        if (bv === null) return -1;
        return (av - bv) * direction;
      }
      return a.code.localeCompare(b.code) * direction;
    });
  }, [items, search, sort, quoteMap]);

  const selectedVisible = rows.filter((row) => selected.has(row.id));
  const allSelected = rows.length > 0 && selectedVisible.length === rows.length;
  const someSelected = selectedVisible.length > 0 && !allSelected;

  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = someSelected;
  }, [someSelected]);

  // --- 候选搜索 ------------------------------------------------------------ //
  const candidates = useQuery({
    queryKey: ["watchlist-add", addQuery.trim()],
    queryFn: () => api.listStocks({ q: addQuery.trim(), limit: 20 }),
    enabled: addQuery.trim().length > 0,
    staleTime: 30_000,
  });

  function flash(noticeValue: Notice) {
    setNotice(noticeValue);
  }

  async function refreshWatchlist() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["watchlist"] }),
      queryClient.invalidateQueries({ queryKey: ["watchlist-lists"] }),
    ]);
  }

  async function handleRefresh() {
    await Promise.all([itemsQuery.refetch(), listsQuery.refetch()]);
    if (codes.length > 0) {
      await Promise.all([quotesQuery.refetch(), scoresQuery.refetch()]);
    }
  }

  async function handleAddCandidate(code: string) {
    if (readOnly || addBusy || existingCodes.has(code)) return;
    setAddBusy(true);
    try {
      await api.addWatch(code, listName);
      setAddQuery("");
      setAddOpen(false);
      await refreshWatchlist();
      flash({ kind: "ok", text: `已加入「${listName}」：${code}` });
    } catch (error) {
      flash({ kind: "error", text: errorText(error) });
    } finally {
      setAddBusy(false);
    }
  }

  function openRemoveConfirm(item: WatchlistItem) {
    if (readOnly || removingId !== null || batchBusy) return;
    if (savingNote) {
      flash({ kind: "warn", text: "备注保存中，请稍后再移出" });
      return;
    }
    setRemoveTarget(item);
  }

  async function confirmRemove() {
    const item = removeTarget;
    if (!item || removingId !== null) return;
    setRemovingId(item.id);
    try {
      await api.removeWatch(item.id);
      setSelected((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
      await refreshWatchlist();
      flash({ kind: "ok", text: `已移出：${item.code}` });
    } catch (error) {
      flash({ kind: "error", text: errorText(error) });
    } finally {
      setRemovingId(null);
      setRemoveTarget(null);
    }
  }

  function openBatchConfirm() {
    if (readOnly || batchBusy || selected.size === 0) return;
    if (savingNote) {
      flash({ kind: "warn", text: "备注保存中，请稍后再批量删除" });
      return;
    }
    setBatchConfirmOpen(true);
  }

  async function confirmBatchRemove() {
    if (batchBusy || selected.size === 0) return;
    const ids = [...selected];
    setBatchBusy(true);
    try {
      const result = await api.batchRemoveWatch(ids);
      setSelected(new Set<number>());
      await refreshWatchlist();
      flash(
        result.missing.length > 0
          ? {
              kind: "warn",
              text: `已移出 ${result.deleted} 只；${result.missing.length} 只已不存在，已刷新`,
            }
          : { kind: "ok", text: `已移出 ${result.deleted} 只` },
      );
    } catch (error) {
      flash({ kind: "error", text: errorText(error) });
    } finally {
      setBatchBusy(false);
      setBatchConfirmOpen(false);
    }
  }

  // --- 备注内联编辑 -------------------------------------------------------- //
  function startEditNote(item: WatchlistItem) {
    if (readOnly) return;
    cancelEditRef.current = false;
    setNoteError(null);
    setNoteDraft(item.note);
    setEditingId(item.id);
  }

  async function saveNote(item: WatchlistItem) {
    if (readOnly || cancelEditRef.current || savingNote) return;
    const note = noteDraft.trim();
    if (note === item.note) {
      setEditingId((current) => (current === item.id ? null : current));
      return;
    }
    setSavingNote(true);
    setNoteError(null);
    try {
      await api.addWatch(item.code, listName, note);
      await refreshWatchlist();
      setEditingId((current) => (current === item.id ? null : current));
      flash({ kind: "ok", text: "备注已保存" });
    } catch (error) {
      setNoteError(errorText(error));
    } finally {
      setSavingNote(false);
    }
  }

  // --- 清单动作 ------------------------------------------------------------ //
  function openNewList() {
    if (readOnly) return;
    setListFormError("");
    setListNameDraft("");
    setListDialogMode("new");
  }

  function openRenameList() {
    if (readOnly) return;
    setListFormError("");
    setListNameDraft(listName);
    setListDialogMode("rename");
  }

  function closeListDialog() {
    if (listBusy) return;
    setListDialogMode(null);
  }

  async function submitListDialog() {
    const trimmed = listNameDraft.trim();
    if (!trimmed) {
      setListFormError("清单名不能为空");
      return;
    }
    if (trimmed.length > MAX_LIST_NAME) {
      setListFormError(`清单名最多 ${MAX_LIST_NAME} 个字符`);
      return;
    }
    if (trimmed !== listName && listOptions.some((option) => option.list_name === trimmed)) {
      setListFormError("同名清单已存在");
      return;
    }
    if (listDialogMode === "new") {
      setDrafts((current) => (current.includes(trimmed) ? current : [...current, trimmed]));
      setListName(trimmed);
      setListDialogMode(null);
      window.setTimeout(() => addInputRef.current?.focus(), 0);
      return;
    }
    if (trimmed === listName) {
      setListDialogMode(null);
      return;
    }
    if (isDraftList) {
      setDrafts((current) => current.map((name) => (name === listName ? trimmed : name)));
      setListName(trimmed);
      setListDialogMode(null);
      return;
    }
    setListBusy(true);
    setListFormError("");
    try {
      const result = await api.renameWatchlistList(listName, trimmed);
      await refreshWatchlist();
      setListName(result.new_name);
      setListDialogMode(null);
      flash({ kind: "ok", text: `清单已重命名为「${result.new_name}」（${result.updated} 只）` });
    } catch (error) {
      setListFormError(errorText(error));
    } finally {
      setListBusy(false);
    }
  }

  function pickNextList(exclude: string): string {
    const remaining = listOptions.filter((option) => option.list_name !== exclude);
    return remaining.find((option) => option.list_name === DEFAULT_LIST)?.list_name ?? remaining[0]?.list_name ?? DEFAULT_LIST;
  }

  function openDeleteDialog() {
    if (readOnly || deleteBusy || batchBusy) return;
    setDeleteDialogOpen(true);
  }

  async function confirmDeleteList() {
    if (alertRefs.length > 0 || refCheckLoading) return;
    const next = pickNextList(listName);
    if (isDraftList) {
      setDrafts((current) => current.filter((name) => name !== listName));
      setListName(next);
      setDeleteDialogOpen(false);
      flash({ kind: "ok", text: `已删除草稿清单「${listName}」` });
      return;
    }
    setDeleteBusy(true);
    try {
      const result = await api.deleteWatchlistList(listName);
      await refreshWatchlist();
      setDrafts((current) => current.filter((name) => name !== listName));
      setListName(next);
      flash({ kind: "ok", text: `已删除清单「${listName}」（${result.deleted} 只）` });
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) {
        await refreshWatchlist();
        setListName(next);
        flash({ kind: "warn", text: "清单已不存在，已刷新" });
      } else {
        flash({ kind: "error", text: errorText(error) });
      }
    } finally {
      setDeleteBusy(false);
      setDeleteDialogOpen(false);
    }
  }

  // --- CSV 导出 ------------------------------------------------------------ //
  function handleExport() {
    if (items.length === 0) return;
    const header = ["code", "name", "note"].join(",");
    const body = items
      .map((item) => [csvCell(item.code), csvCell(item.name), csvCell(item.note)].join(","))
      .join("\r\n");
    const csv = `\uFEFF${header}\r\n${body}`;
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = window.URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `watchlist-${safeFileName(listName)}-${todayISO()}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    window.URL.revokeObjectURL(url);
  }

  // --- CSV 导入 ------------------------------------------------------------ //
  function openImport() {
    if (readOnly) return;
    setImportOpen(true);
    setImportFileName("");
    setImportRows([]);
    setImportError("");
    setImportResult(null);
    setSkipExisting(true);
    setImportTargetMode("existing");
    setImportTargetList(listName);
    setImportTargetNew("");
  }

  function closeImport() {
    if (importBusy) return;
    setImportOpen(false);
  }

  async function handleImportFile(file: File) {
    setImportResult(null);
    setImportError("");
    setImportFileName(file.name);
    setImportRows([]);
    try {
      if (file.size > 1024 * 1024) throw new Error("文件超过 1 MB，请拆分后重试");
      const text = await file.text();
      const records = parseCsvRecords(text);
      if (records.length === 0) throw new Error("CSV 没有数据行");
      const header = records[0].fields.map((field) => field.trim().toLowerCase());
      const codeIndex = header.findIndex((field) => field === "code" || field === "代码");
      if (codeIndex < 0) throw new Error("表头需包含 code/代码 列");
      const nameIndex = header.findIndex((field) => field === "name" || field === "名称");
      const noteIndex = header.findIndex((field) => field === "note" || field === "备注");
      const parsed: CsvRow[] = records
        .slice(1)
        .map((record) => ({
          line: record.line,
          code: (record.fields[codeIndex] ?? "").trim(),
          name: nameIndex >= 0 ? (record.fields[nameIndex] ?? "").trim() : "",
          note: noteIndex >= 0 ? (record.fields[noteIndex] ?? "").trim() : "",
        }))
        .filter((row) => row.code || row.name || row.note);
      if (parsed.length === 0) throw new Error("CSV 没有有效数据行");
      if (parsed.length > MAX_IMPORT_ROWS) throw new Error(`单次最多导入 ${MAX_IMPORT_ROWS} 行`);
      setImportRows(parsed);
    } catch (error) {
      setImportError(errorText(error));
    }
  }

  async function submitImport() {
    if (importBusy || importRows.length === 0) return;
    const targetName = importTargetMode === "new" ? importTargetNew.trim() : importTargetList;
    if (!targetName) {
      setImportError("请选择已有清单，或输入新清单名称");
      return;
    }
    if (targetName.length > MAX_LIST_NAME) {
      setImportError(`清单名最多 ${MAX_LIST_NAME} 个字符`);
      return;
    }
    const notes: Record<string, string> = {};
    importRows.forEach((row) => {
      if (row.note && !(row.code in notes)) notes[row.code] = row.note;
    });
    setImportBusy(true);
    setImportError("");
    try {
      const result = await api.importWatchlist({
        list_name: targetName,
        codes: importRows.map((row) => row.code),
        ...(Object.keys(notes).length > 0 ? { notes } : {}),
        create_list:
          importTargetMode === "new" || !serverLists.some((row) => row.list_name === targetName),
        skip_existing: skipExisting,
      });
      setImportResult(result);
      await refreshWatchlist();
      if (result.list_name !== listName) {
        setListName(result.list_name);
        flash({ kind: "ok", text: `已导入「${result.list_name}」，已切换到该清单` });
      } else {
        flash({ kind: "ok", text: `已导入「${result.list_name}」` });
      }
    } catch (error) {
      const text =
        error instanceof ApiError && error.status === 409
          ? `配额不足或清单不可写：${error.detail}。可减少导入行数，或先清理自选后重试。`
          : errorText(error);
      setImportError(text);
    } finally {
      setImportBusy(false);
    }
  }

  function invalidLine(index: number): number {
    return importRows[index]?.line ?? index + 1;
  }

  function previewStatus(row: CsvRow): string {
    if (!row.code) return "空代码";
    if (existingCodes.has(row.code)) return skipExisting ? "已存在→跳过" : "已存在→更新";
    return "待导入";
  }

  // --- 排序与选择 ---------------------------------------------------------- //
  function toggleSort(field: SortField) {
    setSort((current) => {
      if (current.field === field) {
        return { field, direction: current.direction === "asc" ? "desc" : "asc" };
      }
      return { field, direction: field === "pct_chg" ? "desc" : "asc" };
    });
  }

  function toggleOne(id: number) {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleAll() {
    setSelected((current) => {
      const next = new Set(current);
      if (allSelected) rows.forEach((row) => next.delete(row.id));
      else rows.forEach((row) => next.add(row.id));
      return next;
    });
  }

  function renderSortHeader(field: SortField, label: string, alignRight = false) {
    const active = sort.field === field;
    return (
      <th
        className={`th ${alignRight ? "text-right" : ""}`}
        aria-sort={active ? (sort.direction === "asc" ? "ascending" : "descending") : "none"}
      >
        <button
          type="button"
          className={`inline-flex items-center gap-1 ${alignRight ? "ml-auto" : ""} transition hover:text-slate-200`}
          onClick={() => toggleSort(field)}
          title={`按${label}排序`}
        >
          {label}
          <span className="text-[10px] text-slate-500">
            {active ? (sort.direction === "asc" ? "▲" : "▼") : "▾"}
          </span>
        </button>
      </th>
    );
  }

  const quoteDate = quotesQuery.data?.as_of ?? null;
  const factorDate = scoresQuery.data?.trade_date ?? null;
  const listTotal = listOptions.reduce((sum, option) => sum + option.count, 0);
  const importTargetName = importTargetMode === "new" ? importTargetNew.trim() : importTargetList;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[250px_1fr]">
        {/* 桌面清单栏 */}
        <aside className="card hidden space-y-3 xl:block">
          <div className="flex items-center justify-between">
            <span className="text-sm font-medium text-slate-300">清单</span>
            <button
              type="button"
              className="text-xs text-sky-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
              onClick={openNewList}
              disabled={readOnly}
            >
              ＋ 新建
            </button>
          </div>
          <div className="space-y-1">
            {listOptions.map((option) => {
              const active = option.list_name === listName;
              const count = active && itemsQuery.data ? currentListCount : option.count;
              return (
                <button
                  key={option.list_name}
                  type="button"
                  className={`flex w-full items-center justify-between rounded-md border-l-2 px-2 py-2 text-left text-sm transition ${
                    active
                      ? "border-sky-500 bg-sky-950/40 text-sky-200"
                      : "border-transparent text-slate-300 hover:bg-slate-800/60"
                  }`}
                  onClick={() => setListName(option.list_name)}
                  title={option.list_name}
                >
                  <span className="min-w-0 truncate">{option.list_name}</span>
                  <span className="ml-2 shrink-0 text-xs text-slate-500">{count}</span>
                </button>
              );
            })}
          </div>
          <div className="border-t border-slate-800 pt-2 text-[11px] text-slate-500">
            清单合计 {listTotal} 只；当前清单 {currentListCount} 只。
          </div>
          <div className="flex flex-wrap gap-2 border-t border-slate-800 pt-2">
            <button
              type="button"
              className="text-xs text-sky-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
              onClick={openRenameList}
              disabled={readOnly}
            >
              重命名
            </button>
            <button
              type="button"
              className="text-xs text-red-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
              onClick={openDeleteDialog}
              disabled={readOnly || batchBusy || deleteBusy}
            >
              删除清单
            </button>
          </div>
          {listsQuery.isError && (
            <p className="text-[11px] text-amber-300">
              清单加载失败：{errorText(listsQuery.error)}
              <button
                type="button"
                className="ml-1 text-sky-400 hover:underline"
                onClick={() => void listsQuery.refetch()}
              >
                重试
              </button>
            </p>
          )}
        </aside>

        {/* 主区 */}
        <section className="card space-y-3">
          {/* 移动端清单切换与操作 */}
          <div className="flex flex-wrap items-center gap-2 xl:hidden">
            <select
              className="input max-w-[13rem]"
              value={listName}
              onChange={(event) => setListName(event.target.value)}
              aria-label="切换自选清单"
            >
              {listOptions.map((option) => (
                <option key={option.list_name} value={option.list_name}>
                  {option.list_name}（{option.list_name === listName && itemsQuery.data ? currentListCount : option.count}）
                </option>
              ))}
            </select>
            <button
              type="button"
              className="btn px-2 py-1 text-xs"
              onClick={openNewList}
              disabled={readOnly}
            >
              ＋清单
            </button>
            <button
              type="button"
              className="btn px-2 py-1 text-xs"
              onClick={openRenameList}
              disabled={readOnly}
            >
              重命名
            </button>
            <button
              type="button"
              className="btn px-2 py-1 text-xs"
              onClick={openDeleteDialog}
              disabled={readOnly || batchBusy || deleteBusy}
            >
              删除
            </button>
          </div>

          {/* 添加 / 搜索 / 操作 */}
          <div className="flex flex-wrap items-center gap-2">
            <div className="relative w-full sm:w-64">
              <input
                ref={addInputRef}
                className="input w-full"
                value={addQuery}
                placeholder={`加入「${listName}」：输入代码或名称`}
                disabled={readOnly}
                onChange={(event) => {
                  setAddQuery(event.target.value);
                  setAddOpen(true);
                }}
                onFocus={() => setAddOpen(true)}
                onBlur={() => window.setTimeout(() => setAddOpen(false), 120)}
                aria-label="添加自选：搜索代码或名称"
              />
              {addOpen && addQuery.trim().length > 0 && (
                <div className="absolute z-20 mt-1 max-h-64 w-full overflow-y-auto rounded-md border border-slate-700 bg-slate-900 shadow-xl">
                  {candidates.isFetching && <div className="px-3 py-2 text-xs text-slate-500">搜索中…</div>}
                  {candidates.isError && (
                    <div className="px-3 py-2 text-xs text-amber-300">
                      搜索失败：{errorText(candidates.error)}
                    </div>
                  )}
                  {!candidates.isFetching &&
                    !candidates.isError &&
                    candidates.data?.items.length === 0 && (
                      <div className="px-3 py-2 text-xs text-slate-500">
                        没有匹配的标的，试试完整代码（如 600519.SH）
                      </div>
                    )}
                  {candidates.data?.items.map((stock) => {
                    const exists = existingCodes.has(stock.code);
                    return (
                      <button
                        key={stock.code}
                        type="button"
                        className="flex w-full items-center gap-3 px-3 py-2 text-left text-sm hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50"
                        disabled={exists || addBusy}
                        onMouseDown={(event) => {
                          event.preventDefault();
                          void handleAddCandidate(stock.code);
                        }}
                      >
                        <span className="font-mono text-xs text-slate-400">{stock.code}</span>
                        <span className="min-w-0 flex-1 truncate text-slate-200">{stock.name}</span>
                        <span className={`text-xs ${exists ? "text-slate-500" : "text-sky-400"}`}>
                          {exists ? "已在清单" : "添加"}
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>
            <input
              className="input w-full sm:w-52"
              value={search}
              placeholder="搜索代码/名称/备注"
              onChange={(event) => setSearch(event.target.value)}
              aria-label="筛选自选"
            />
            <span className="text-xs text-slate-500">
              共 {currentListCount} 只
              {search.trim() ? ` · 匹配 ${rows.length} 只` : ""}
            </span>
            <div className="ml-auto flex flex-wrap items-center gap-2">
              <button
                type="button"
                className="btn px-2 py-1 text-xs"
                onClick={handleExport}
                disabled={items.length === 0}
              >
                导出 CSV
              </button>
              <button
                type="button"
                className="btn px-2 py-1 text-xs"
                onClick={openImport}
                disabled={readOnly}
              >
                导入 CSV
              </button>
              <button
                type="button"
                className="btn px-2 py-1 text-xs"
                onClick={() => void handleRefresh()}
                disabled={itemsQuery.isFetching}
              >
                {itemsQuery.isFetching ? "刷新中…" : "刷新"}
              </button>
              <label
                className="flex items-center gap-1 text-xs text-slate-400"
                title="开启后每 60 秒刷新清单、行情与评分"
              >
                <input
                  type="checkbox"
                  checked={autoRefresh}
                  onChange={(event) => setAutoRefresh(event.target.checked)}
                  aria-label="自动刷新"
                />
                自动刷新
              </label>
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
            {quoteDate && (
              <span>
                {quotesQuery.data?.source === "snapshot" ? "行情快照" : "日线数据"} · 数据日期 {quoteDate}
              </span>
            )}
            {factorDate && <span>因子日期 {factorDate}</span>}
            {quotesQuery.isFetching && <span>行情加载中…</span>}
            {scoresQuery.isError && <span>评分暂不可用</span>}
          </div>

          {readOnly && (
            <p className="text-xs text-amber-300">
              当前工作区为只读角色（viewer）：可查看清单、行情与备注，不能新增、编辑、导入或删除自选。
            </p>
          )}
          {notice && (
            <p
              role={notice.kind === "error" ? "alert" : "status"}
              className={`text-xs ${
                notice.kind === "error"
                  ? "text-red-400"
                  : notice.kind === "warn"
                    ? "text-amber-300"
                    : "text-sky-300"
              }`}
            >
              {notice.text}
            </p>
          )}
          {listsQuery.isError && (
            <p className="text-xs text-amber-300 xl:hidden">
              清单加载失败：{errorText(listsQuery.error)}
              <button
                type="button"
                className="ml-1 text-sky-400 hover:underline"
                onClick={() => void listsQuery.refetch()}
              >
                重试
              </button>
            </p>
          )}
          {quotesQuery.isError && (
            <div className="flex flex-wrap items-center gap-2 rounded-md border border-amber-900/60 bg-amber-950/40 px-3 py-2 text-xs text-amber-300">
              行情暂不可用：{errorText(quotesQuery.error)}。清单管理、备注与 CSV 仍可用。
              <button
                type="button"
                className="text-sky-400 hover:underline"
                onClick={() => void quotesQuery.refetch()}
              >
                重试
              </button>
            </div>
          )}

          {itemsQuery.isError ? (
            <div className="flex flex-wrap items-center gap-3 text-sm text-amber-300">
              自选加载失败：{errorText(itemsQuery.error)}
              <button
                type="button"
                className="btn px-2 py-1 text-xs"
                onClick={() => void itemsQuery.refetch()}
              >
                重试
              </button>
            </div>
          ) : itemsQuery.isLoading ? (
            <p className="text-sm text-slate-500">加载中…</p>
          ) : items.length === 0 ? (
            <div className="py-8 text-center">
              <p className="text-sm text-slate-400">「{listName}」还没有股票。</p>
              <p className="mt-1 text-xs text-slate-500">
                {isDraftList
                  ? "这是本地草稿清单，加入第一只股票后才会保存到工作区。"
                  : "在行情页找到股票，或在上方输入代码/名称直接添加。"}
              </p>
              <div className="mt-3 flex flex-wrap justify-center gap-2">
                <button type="button" className="btn btn-primary" onClick={() => navigate("/market")}>
                  去行情页挑几只
                </button>
                <button
                  type="button"
                  className="btn"
                  disabled={readOnly}
                  onClick={() => {
                    addInputRef.current?.focus();
                    setAddOpen(true);
                  }}
                >
                  添加自选
                </button>
              </div>
            </div>
          ) : rows.length === 0 ? (
            <div className="py-6 text-center text-sm text-slate-500">
              没有匹配「{search.trim()}」的自选。
              <button
                type="button"
                className="ml-2 text-xs text-sky-400 hover:underline"
                onClick={() => setSearch("")}
              >
                清除搜索
              </button>
            </div>
          ) : (
            <>
              <div className="table-wrap">
                <table className="w-full min-w-[1040px]">
                  <thead>
                    <tr className="border-b border-slate-800">
                      <th className="th w-8">
                        <input
                          ref={selectAllRef}
                          type="checkbox"
                          checked={allSelected}
                          disabled={readOnly || rows.length === 0}
                          onChange={toggleAll}
                          aria-label="全选当前清单"
                        />
                      </th>
                      {renderSortHeader("code", "代码")}
                      {renderSortHeader("name", "名称")}
                      <th className="th text-right">最新价</th>
                      {renderSortHeader("pct_chg", "涨跌幅", true)}
                      <th className="th text-right">成交额</th>
                      <th className="th text-right">换手</th>
                      <th className="th text-right" title={factorDate ? `因子日期 ${factorDate}` : "暂无因子数据"}>
                        评分
                      </th>
                      <th className="th">备注</th>
                      <th className="th text-right">操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((item) => {
                      const quote = quoteMap.get(item.code);
                      const score = scoreMap.get(item.code);
                      return (
                        <tr key={item.id} className="border-t border-slate-800/60 hover:bg-slate-800/30">
                          <td className="td">
                            <input
                              type="checkbox"
                              checked={selected.has(item.id)}
                              disabled={readOnly}
                              onChange={() => toggleOne(item.id)}
                              aria-label={`选择 ${item.name || item.code} ${item.code}`}
                            />
                          </td>
                          <td className="td">
                            <button
                              type="button"
                              className="font-mono text-xs text-sky-400 hover:underline"
                              onClick={() => {
                                useViewState.getState().setMarket({ selected: item.code });
                                navigate("/market");
                              }}
                              title="在行情页查看 K 线"
                            >
                              {item.code}
                            </button>
                          </td>
                          <td className="td text-slate-300">{item.name || "--"}</td>
                          <td className="td text-right text-slate-200">{formatNumber(quote?.price, 2)}</td>
                          <td className={`td text-right ${changeColor(quote?.pct_chg)}`}>
                            {quote?.stale && (
                              <span
                                className="badge mr-1 bg-amber-900/50 text-amber-300"
                                title={quote.trade_date ? `最后数据 ${quote.trade_date}` : "旧数据"}
                              >
                                旧
                              </span>
                            )}
                            {formatPct(quote?.pct_chg)}
                          </td>
                          <td className="td text-right text-slate-300">{formatCompact(quote?.amount)}</td>
                          <td className="td text-right text-slate-300">
                            {quote?.turnover === null || quote?.turnover === undefined
                              ? "--"
                              : `${quote.turnover.toFixed(2)}%`}
                          </td>
                          <td className="td text-right">
                            <span
                              className={score?.score === null || score?.score === undefined ? "text-slate-500" : "text-sky-300"}
                              title={score?.trade_date ? `因子日期 ${score.trade_date}` : "暂无评分"}
                            >
                              {formatScore(score?.score)}
                            </span>
                          </td>
                          <td className="td max-w-[12rem]">
                            {editingId === item.id ? (
                              <div className="flex items-center gap-1">
                                <input
                                  className="input w-40 py-0.5 text-xs"
                                  value={noteDraft}
                                  maxLength={256}
                                  autoFocus
                                  disabled={savingNote}
                                  onChange={(event) => setNoteDraft(event.target.value)}
                                  onBlur={() => {
                                    if (!cancelEditRef.current) void saveNote(item);
                                  }}
                                  onKeyDown={(event) => {
                                    if (event.key === "Enter") {
                                      event.preventDefault();
                                      void saveNote(item);
                                    } else if (event.key === "Escape") {
                                      event.preventDefault();
                                      cancelEditRef.current = true;
                                      setEditingId(null);
                                      setNoteError(null);
                                    }
                                  }}
                                  aria-label={`编辑 ${item.code} 的备注`}
                                />
                                {savingNote && <span className="text-[10px] text-slate-500">保存中…</span>}
                                {noteError && <span className="text-[10px] text-red-400">{noteError}</span>}
                              </div>
                            ) : (
                              <button
                                type="button"
                                className={`w-full truncate text-left text-xs ${
                                  item.note ? "text-slate-300" : "text-slate-500"
                                } ${readOnly ? "cursor-default" : "hover:text-sky-300"}`}
                                onClick={() => startEditNote(item)}
                                disabled={readOnly}
                                title={
                                  readOnly
                                    ? "查看者无权修改备注"
                                    : "点击编辑备注（Enter 保存 / Esc 取消）"
                                }
                              >
                                {item.note || "添加备注"}
                              </button>
                            )}
                          </td>
                          <td className="td text-right">
                            <div className="flex flex-wrap justify-end gap-x-2 gap-y-1">
                              <button
                                type="button"
                                className="text-xs text-sky-400 hover:underline"
                                onClick={() => {
                                  useViewState.getState().setMarket({ selected: item.code });
                                  navigate("/market");
                                }}
                              >
                                K线
                              </button>
                              <button
                                type="button"
                                className="text-xs text-sky-400 hover:underline"
                                onClick={() => {
                                  useViewState.getState().setAlertForm({
                                    name: `自选预警 · ${item.name || item.code}`,
                                    rule_type: "price_cross",
                                    scope_type: "codes",
                                    scope_value: item.code,
                                  });
                                  navigate("/alerts");
                                }}
                              >
                                预警
                              </button>
                              <button
                                type="button"
                                className="text-xs text-sky-400 hover:underline"
                                onClick={() => {
                                  useViewState.getState().setBacktest({ codes: item.code });
                                  navigate("/backtest");
                                }}
                              >
                                回测
                              </button>
                              <button
                                type="button"
                                className="text-xs text-red-400 hover:underline disabled:cursor-not-allowed disabled:opacity-40"
                                disabled={readOnly || removingId !== null || batchBusy || savingNote}
                                onClick={() => openRemoveConfirm(item)}
                              >
                                {removingId === item.id ? "移出中…" : "移出"}
                              </button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>

              <div className="flex flex-wrap items-center gap-2 text-xs text-slate-400">
                <span>
                  已选 {selected.size} 项
                  {selected.size !== selectedVisible.length ? `（当前可见 ${selectedVisible.length}）` : ""}
                </span>
                <button
                  type="button"
                  className="btn px-2 py-1 text-xs"
                  disabled={readOnly || selected.size === 0 || batchBusy || savingNote}
                  onClick={openBatchConfirm}
                >
                  {batchBusy ? "处理中…" : "批量删除"}
                </button>
                {selected.size > 0 && (
                  <button
                    type="button"
                    className="btn px-2 py-1 text-xs"
                    disabled={batchBusy}
                    onClick={() => setSelected(new Set<number>())}
                  >
                    取消选择
                  </button>
                )}
              </div>
            </>
          )}
        </section>
      </div>

      {/* 新建 / 重命名清单弹层 */}
      {listDialogMode && (
        <div
          className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 px-4 py-6"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeListDialog();
          }}
        >
          <form
            className="w-full max-w-[420px] space-y-3 rounded-lg border border-slate-800 bg-slate-900 p-4 shadow-2xl shadow-black/40"
            role="dialog"
            aria-modal="true"
            aria-labelledby="watchlist-list-dialog-title"
            onSubmit={(event) => {
              event.preventDefault();
              void submitListDialog();
            }}
          >
            <div id="watchlist-list-dialog-title" className="text-sm font-medium text-slate-200">
              {listDialogMode === "new" ? "新建清单" : `重命名「${listName}」`}
            </div>
            <div>
              <label className="label" htmlFor="watchlist-list-name">
                清单名 *
              </label>
              <input
                id="watchlist-list-name"
                className="input w-full"
                value={listNameDraft}
                maxLength={MAX_LIST_NAME}
                autoFocus
                onChange={(event) => setListNameDraft(event.target.value)}
                placeholder="如 观察池-1017"
              />
              <p className="mt-1 text-[11px] text-slate-500">
                {listDialogMode === "new"
                  ? "空清单只保存在本地，加入第一只股票后才落库。"
                  : "引用旧清单名的预警规则不会自动跟随。"}
              </p>
              {listDialogMode === "rename" && (
                <div className="mt-2 rounded-md border border-amber-900/60 bg-amber-950/40 px-2 py-2 text-[11px] leading-relaxed text-amber-300">
                  {refCheckLoading ? (
                    "正在检查预警规则引用…"
                  ) : refCheckFailed && alertRefs.length === 0 ? (
                    "引用检查失败，无法确认是否存在预警引用；重命名后请手动检查。"
                  ) : (
                    <>
                      {alertRefs.length > 0
                        ? `有 ${alertRefs.length} 条预警规则引用该清单，重命名不会自动更新它们，请记得手动修改。`
                        : "未发现预警规则引用该清单。"}
                      {refCheckFailed && " 引用检查失败，无法确认是否存在引用。"}
                    </>
                  )}
                </div>
              )}
            </div>
            {listFormError && (
              <p role="alert" className="text-xs text-red-400">
                {listFormError}
              </p>
            )}
            <div className="flex justify-end gap-2">
              <button type="button" className="btn" onClick={closeListDialog} disabled={listBusy}>
                取消
              </button>
              <button type="submit" className="btn btn-primary" disabled={listBusy}>
                {listBusy ? "保存中…" : "保存"}
              </button>
            </div>
          </form>
        </div>
      )}

      {/* CSV 导入弹层 */}
      {importOpen && (
        <div
          className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 px-4 py-6"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget) closeImport();
          }}
        >
          <div
            className="flex max-h-[85vh] w-full max-w-[720px] flex-col overflow-hidden rounded-lg border border-slate-800 bg-slate-900 p-4 shadow-2xl shadow-black/40"
            role="dialog"
            aria-modal="true"
            aria-labelledby="watchlist-import-title"
          >
            <div className="flex items-start justify-between gap-3">
              <div>
                <div id="watchlist-import-title" className="text-sm font-medium text-slate-200">
                  导入 CSV
                </div>
                <p className="mt-1 text-[11px] text-slate-500">
                  目标清单「{importTargetName || "未选择"}」· 列 code,name,note（或 代码,名称,备注）· UTF-8
                </p>
              </div>
              <button type="button" className="btn px-2 py-1 text-xs" onClick={closeImport} disabled={importBusy}>
                关闭
              </button>
            </div>

            {importResult ? (
              <div className="mt-3 space-y-3 overflow-y-auto">
                <div className="flex flex-wrap gap-2 text-xs">
                  <span className="badge bg-emerald-900/50 text-emerald-300">新增 {importResult.added}</span>
                  <span className="badge bg-sky-900/50 text-sky-300">更新 {importResult.updated}</span>
                  <span className="badge bg-slate-800 text-slate-300">跳过 {importResult.skipped}</span>
                  <span
                    className={`badge ${
                      importResult.invalid.length > 0
                        ? "bg-amber-900/50 text-amber-300"
                        : "bg-slate-800 text-slate-300"
                    }`}
                  >
                    非法 {importResult.invalid.length}
                  </span>
                </div>
                <p className="text-[11px] text-slate-500">
                  目标清单「{importResult.list_name}」共 {importResult.total_items} 只
                  {importResult.quota
                    ? ` · 配额 ${importResult.quota.used}/${importResult.quota.limit}`
                    : ""}
                </p>
                {importResult.invalid.length > 0 && (
                  <div className="max-h-40 overflow-auto rounded-md border border-slate-800 bg-slate-950/60 p-2">
                    <div className="mb-1 text-[11px] text-slate-400">未导入的行</div>
                    {importResult.invalid.map((item) => (
                      <p key={`${item.index}-${item.code}`} className="text-[11px] text-amber-300">
                        第 {invalidLine(item.index)} 行 {item.code || "（空代码）"}：{item.message || item.reason}
                      </p>
                    ))}
                  </div>
                )}
                <div className="flex justify-end">
                  <button type="button" className="btn btn-primary" onClick={closeImport}>
                    完成
                  </button>
                </div>
              </div>
            ) : (
              <div className="mt-3 space-y-3 overflow-y-auto">
                <div className="space-y-2 rounded-md border border-slate-800 bg-slate-950/40 p-2">
                  <div className="text-[11px] text-slate-400">目标清单</div>
                  <label className="flex items-center gap-2 text-xs text-slate-300">
                    <input
                      type="radio"
                      name="import-target"
                      checked={importTargetMode === "existing"}
                      onChange={() => setImportTargetMode("existing")}
                      disabled={importBusy}
                    />
                    已有清单
                  </label>
                  {importTargetMode === "existing" && (
                    <select
                      className="input w-full"
                      value={importTargetList}
                      onChange={(event) => setImportTargetList(event.target.value)}
                      disabled={importBusy}
                      aria-label="选择导入目标清单"
                    >
                      {listOptions.map((option) => (
                        <option key={option.list_name} value={option.list_name}>
                          {option.list_name}（{option.count} 只）
                        </option>
                      ))}
                    </select>
                  )}
                  <label className="flex items-center gap-2 text-xs text-slate-300">
                    <input
                      type="radio"
                      name="import-target"
                      checked={importTargetMode === "new"}
                      onChange={() => setImportTargetMode("new")}
                      disabled={importBusy}
                    />
                    新建清单
                  </label>
                  {importTargetMode === "new" && (
                    <input
                      className="input w-full"
                      value={importTargetNew}
                      maxLength={MAX_LIST_NAME}
                      placeholder="如 观察池-1017"
                      onChange={(event) => setImportTargetNew(event.target.value)}
                      disabled={importBusy}
                      aria-label="新清单名称"
                    />
                  )}
                  <p className="text-[11px] text-slate-500">
                    导入到新建清单时，清单会在首次成功导入后落库；成功后若目标不是当前清单，页面会自动切换过去。
                  </p>
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  <label className="btn cursor-pointer px-2 py-1 text-xs">
                    选择 CSV 文件
                    <input
                      ref={importInputRef}
                      type="file"
                      accept=".csv,text/csv"
                      className="hidden"
                      onChange={(event) => {
                        const file = event.target.files?.[0];
                        event.target.value = "";
                        if (file) void handleImportFile(file);
                      }}
                    />
                  </label>
                  {importFileName && <span className="text-xs text-slate-400">{importFileName}</span>}
                  <label className="ml-auto flex items-center gap-2 text-xs text-slate-300">
                    <input
                      type="checkbox"
                      checked={skipExisting}
                      onChange={(event) => setSkipExisting(event.target.checked)}
                      disabled={importBusy}
                    />
                    已存在的股票跳过
                  </label>
                </div>

                {importRows.length > 0 && (
                  <>
                    <div className="text-xs text-slate-500">
                      共 {importRows.length} 行，预览前 {Math.min(20, importRows.length)} 行：
                    </div>
                    <div className="max-h-64 overflow-auto rounded-md border border-slate-800">
                      <table className="w-full min-w-[620px]">
                        <thead className="bg-slate-900">
                          <tr>
                            <th className="th">行</th>
                            <th className="th">代码</th>
                            <th className="th">名称</th>
                            <th className="th">备注</th>
                            <th className="th">状态</th>
                          </tr>
                        </thead>
                        <tbody>
                          {importRows.slice(0, 20).map((row) => (
                            <tr key={`${row.line}-${row.code}`} className="border-t border-slate-800/60">
                              <td className="td text-slate-500">{row.line}</td>
                              <td className="td font-mono text-xs">{row.code || "--"}</td>
                              <td className="td text-slate-400">{row.name || "--"}</td>
                              <td className="td max-w-[12rem] truncate text-slate-400" title={row.note}>
                                {row.note || "--"}
                              </td>
                              <td className="td text-xs text-slate-400">{previewStatus(row)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                )}

                {importError && (
                  <p
                    role="alert"
                    className="rounded-md border border-red-900/60 bg-red-950/40 px-3 py-2 text-xs leading-relaxed text-red-300"
                  >
                    {importError}
                  </p>
                )}

                <div className="flex justify-end gap-2">
                  <button type="button" className="btn" onClick={closeImport} disabled={importBusy}>
                    取消
                  </button>
                  <button
                    type="button"
                    className="btn btn-primary"
                    onClick={() => void submitImport()}
                    disabled={importBusy || importRows.length === 0}
                  >
                    {importBusy ? "导入中…" : `导入 ${importRows.length} 行`}
                  </button>
                </div>
                <p className="text-[11px] text-slate-500">
                  单次最多 {MAX_IMPORT_ROWS} 行、文件 ≤1 MB；非法行会逐行报告，不中断其余行。
                </p>
              </div>
            )}
          </div>
        </div>
      )}

      {/* 统一确认弹层：单条移出 / 批量删除 / 删除清单 */}
      <ConfirmDialog
        open={removeTarget !== null}
        title={removeTarget ? `移出「${removeTarget.code}」？` : "移出自选"}
        description={
          removeTarget ? (
            <>
              将从「{listName}」移出 {removeTarget.code} {removeTarget.name || ""}；备注一并删除。
              行情、预警规则、回测历史不受影响。
            </>
          ) : null
        }
        confirmLabel="移出"
        danger
        busy={removingId !== null}
        onCancel={() => {
          if (removingId === null) setRemoveTarget(null);
        }}
        onConfirm={() => void confirmRemove()}
      />
      <ConfirmDialog
        open={batchConfirmOpen}
        title="批量移出自选"
        description={`将从「${listName}」移出已选中的 ${selected.size} 只自选，备注一并删除。行情、预警规则、回测历史不受影响。`}
        confirmLabel={`移出 ${selected.size} 只`}
        danger
        busy={batchBusy}
        onCancel={() => {
          if (!batchBusy) setBatchConfirmOpen(false);
        }}
        onConfirm={() => void confirmBatchRemove()}
      />

      {deleteDialogOpen && (refCheckLoading || alertRefs.length > 0) && (
        <div
          className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 px-4"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !deleteBusy) setDeleteDialogOpen(false);
          }}
        >
          <div
            role="dialog"
            aria-modal="true"
            aria-labelledby="watchlist-delete-blocked-title"
            className="w-full max-w-[460px] rounded-lg border border-slate-800 bg-slate-900 p-4 shadow-2xl"
          >
            <h2 id="watchlist-delete-blocked-title" className="text-sm font-medium text-slate-100">
              {refCheckLoading ? `正在检查「${listName}」的引用…` : `无法删除清单「${listName}」`}
            </h2>
            <div className="mt-2 space-y-1 text-xs leading-relaxed text-slate-400">
              {refCheckLoading ? (
                <p>正在检查预警规则引用，请稍候…</p>
              ) : (
                <>
                  <p>
                    有 {alertRefs.length} 条预警规则引用该清单：
                    {alertRefs
                      .slice(0, 3)
                      .map((rule) => rule.name)
                      .join("、")}
                    {alertRefs.length > 3 ? " 等" : ""}。
                  </p>
                  <p className="text-amber-300">
                    请先到预警页把这些规则的清单范围改到其他清单，或删除/停用规则后再删除本清单。
                  </p>
                  {refCheckFailed && (
                    <p className="text-amber-300">引用检查失败，无法确认完整引用情况。</p>
                  )}
                </>
              )}
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <button
                type="button"
                className="btn px-3 py-1.5 text-xs"
                onClick={() => setDeleteDialogOpen(false)}
                disabled={deleteBusy}
              >
                关闭
              </button>
              <button
                type="button"
                className="btn cursor-not-allowed border-red-900 bg-red-950/50 px-3 py-1.5 text-xs text-red-300 opacity-60"
                disabled
              >
                {refCheckLoading ? "检查中…" : "删除清单（先修改引用规则）"}
              </button>
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={deleteDialogOpen && !refCheckLoading && alertRefs.length === 0}
        title={isDraftList ? `删除草稿清单「${listName}」？` : `删除清单「${listName}」？`}
        description={
          <>
            <p>
              {isDraftList
                ? "该草稿清单还没有落库，删除只影响本地，不会改动服务端数据。"
                : `将删除「${listName}」并移出 ${items.length} 只自选（备注一并删除）；行情、预警规则、回测历史不受影响。`}
            </p>
            {refCheckLoading && <p className="mt-1">正在检查预警规则引用…</p>}
            {refCheckFailed && (
              <p className="mt-1 text-amber-300">引用检查失败，无法确认是否被预警引用；可稍后重试。</p>
            )}
          </>
        }
        confirmLabel="删除清单"
        danger
        busy={deleteBusy}
        onCancel={() => {
          if (!deleteBusy) setDeleteDialogOpen(false);
        }}
        onConfirm={() => void confirmDeleteList()}
      />
    </div>
  );
}
