/**
 * 证券代码输入框（带「代码 / 名称」联想）。
 *
 * 直接复用 `GET /stocks` 的搜索能力：输入即检索，下拉列出匹配的代码与名称，
 * 点选或回车即写回。`multiple` 为真时把整体输入视为逗号分隔的多标的清单，
 * 联想只作用于光标所在的最后一段（如 "600519.SH, 0000" 以 "0000" 检索），
 * 选中后仅替换该段、前面的代码原样保留——便于「选完一只接着选下一只」。
 *
 * 组件只负责输入与联想，值由调用方写进各自的表单状态，避免把各页面的提交逻辑
 * 耦合进来。候选来源与行情页/自选页的「加入自选」搜索保持一致（股票为主，
 * 预警的指数范围可传 `includeIndices` 打开指数）。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";

interface SymbolInputProps {
  value: string;
  onChange: (value: string) => void;
  /** 逗号分隔的多标的输入；联想作用于最后一段并保留已录入的代码。 */
  multiple?: boolean;
  /** 是否把指数纳入候选（如预警的指数成分范围）；默认只搜股票。 */
  includeIndices?: boolean;
  /** 外层容器宽度类（如 "w-full sm:w-48"）；input 自身始终占满容器。 */
  className?: string;
  placeholder?: string;
  disabled?: boolean;
  id?: string;
  title?: string;
  "aria-label"?: string;
}

/** 候选条数上限：够看清即可，避免长列表拖慢下拉渲染。 */
const LIMIT = 20;
/** 失焦后延迟关闭，留给 onMouseDown 一点时间完成点选。 */
const BLUR_DELAY_MS = 120;

export default function SymbolInput({
  value,
  onChange,
  multiple = false,
  includeIndices = false,
  className = "",
  placeholder,
  disabled,
  id,
  title,
  "aria-label": ariaLabel,
}: SymbolInputProps) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const blurTimer = useRef<number | null>(null);

  const tokens = useMemo(() => value.split(","), [value]);
  const query = (multiple ? tokens[tokens.length - 1] : value).trim();
  const prefix = useMemo(
    () =>
      multiple
        ? tokens
            .slice(0, -1)
            .map((part) => part.trim())
            .filter(Boolean)
        : [],
    [multiple, tokens],
  );
  const existing = useMemo(() => new Set(prefix), [prefix]);

  const results = useQuery({
    queryKey: ["symbolSearch", query, includeIndices],
    queryFn: () => api.listStocks({ q: query, limit: LIMIT, include_indices: includeIndices }),
    enabled: open && query.length > 0,
    staleTime: 30_000,
  });

  const items = results.data?.items ?? [];
  // 换关键词后旧的高亮下标已无意义，回到第一条。
  useEffect(() => {
    setActive(0);
  }, [query]);

  useEffect(
    () => () => {
      if (blurTimer.current !== null) window.clearTimeout(blurTimer.current);
    },
    [],
  );

  function pick(code: string) {
    onChange(multiple ? [...prefix, code].join(", ") : code);
    setOpen(false);
  }

  function closeSoon() {
    if (blurTimer.current !== null) window.clearTimeout(blurTimer.current);
    blurTimer.current = window.setTimeout(() => setOpen(false), BLUR_DELAY_MS);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") {
      setOpen(false);
      return;
    }
    if (!open || items.length === 0) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActive((index) => (index + 1) % items.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActive((index) => (index - 1 + items.length) % items.length);
    } else if (event.key === "Enter") {
      const chosen = items[active];
      if (chosen && !existing.has(chosen.code)) {
        event.preventDefault();
        pick(chosen.code);
      }
    }
  }

  const showList = open && query.length > 0;

  return (
    <div className={`relative ${className}`}>
      <input
        id={id}
        className="input w-full"
        value={value}
        placeholder={placeholder}
        disabled={disabled}
        title={title}
        aria-label={ariaLabel}
        role="combobox"
        aria-expanded={showList}
        aria-autocomplete="list"
        autoComplete="off"
        spellCheck={false}
        onChange={(event) => {
          onChange(event.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onBlur={closeSoon}
        onKeyDown={handleKeyDown}
      />
      {showList && (
        <div
          role="listbox"
          className="absolute z-20 mt-1 max-h-64 w-full overflow-y-auto rounded-md border border-slate-700 bg-slate-900 shadow-xl"
        >
          {results.isFetching && <div className="px-3 py-2 text-xs text-slate-500">搜索中…</div>}
          {results.isError && (
            <div className="px-3 py-2 text-xs text-amber-300">
              搜索失败：{(results.error as Error).message}
            </div>
          )}
          {!results.isFetching && !results.isError && items.length === 0 && (
            <div className="px-3 py-2 text-xs text-slate-500">
              没有匹配的标的，可直接输入完整代码（如 600519.SH）
            </div>
          )}
          {items.map((item, index) => {
            const selected = existing.has(item.code);
            return (
              <button
                key={item.code}
                type="button"
                role="option"
                aria-selected={index === active}
                className={`flex w-full items-center gap-3 px-3 py-2 text-left text-sm hover:bg-slate-800 disabled:cursor-not-allowed disabled:opacity-50 ${
                  index === active ? "bg-slate-800" : ""
                }`}
                disabled={selected}
                onMouseDown={(event) => {
                  event.preventDefault();
                  pick(item.code);
                }}
                onMouseEnter={() => setActive(index)}
              >
                <span className="font-mono text-xs text-slate-400">{item.code}</span>
                <span className="min-w-0 flex-1 truncate text-slate-200">{item.name}</span>
                {selected && <span className="text-xs text-slate-500">已选</span>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
