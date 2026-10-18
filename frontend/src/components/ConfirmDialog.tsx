/** 统一确认弹层。
 *
 * 替代散落的 `window.confirm`：危险操作需要有标题、影响面说明、可选“我已了解”
 * 勾选与 busy 态。调用方负责控制 open 与持有 mutation；本组件只负责呈现与焦点。
 */

import { ReactNode, useEffect, useRef, useState } from "react";

export interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description?: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  busy?: boolean;
  /** 高风险操作（如删除被引用的清单）要求先勾选确认。 */
  requireAcknowledge?: boolean;
  acknowledgeLabel?: string;
  onCancel: () => void;
  onConfirm: () => void;
}

export default function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = "确认",
  cancelLabel = "取消",
  danger = false,
  busy = false,
  requireAcknowledge = false,
  acknowledgeLabel = "我已了解上述影响，继续操作",
  onCancel,
  onConfirm,
}: ConfirmDialogProps) {
  const [acknowledged, setAcknowledged] = useState(false);
  const cancelRef = useRef<HTMLButtonElement | null>(null);

  useEffect(() => {
    if (!open) return;
    setAcknowledged(false);
    // 默认把焦点放在“取消”上：危险操作不应一键误确认。
    requestAnimationFrame(() => cancelRef.current?.focus());
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) onCancel();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, busy, onCancel]);

  if (!open) return null;

  const canConfirm = !busy && (!requireAcknowledge || acknowledged);

  return (
    <div
      className="fixed inset-0 z-40 flex items-center justify-center bg-black/60 px-4"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onCancel();
      }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        className="w-full max-w-[440px] rounded-lg border border-slate-800 bg-slate-900 p-4 shadow-2xl"
      >
        <h2 id="confirm-dialog-title" className="text-sm font-medium text-slate-100">
          {title}
        </h2>
        {description && <div className="mt-2 text-xs leading-relaxed text-slate-400">{description}</div>}

        {requireAcknowledge && (
          <label className="mt-3 flex cursor-pointer items-start gap-2 text-xs text-slate-300">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={acknowledged}
              disabled={busy}
              onChange={(event) => setAcknowledged(event.target.checked)}
            />
            <span>{acknowledgeLabel}</span>
          </label>
        )}

        <div className="mt-4 flex justify-end gap-2">
          <button
            ref={cancelRef}
            type="button"
            className="btn px-3 py-1.5 text-xs"
            onClick={onCancel}
            disabled={busy}
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            className={`btn px-3 py-1.5 text-xs ${
              danger
                ? "border-red-700 bg-red-900/60 text-red-100 hover:border-red-500 hover:bg-red-900"
                : "btn-primary"
            }`}
            onClick={onConfirm}
            disabled={!canConfirm}
          >
            {busy ? "处理中…" : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
