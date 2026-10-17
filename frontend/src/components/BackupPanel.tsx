import { useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { formatDateTime } from "../format";
import type { RestoreInspection } from "../types";

/** 字节数 → 人类可读（备份普遍在 MB 量级，GB 只在全市场库出现）。 */
function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(2)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

/**
 * 系统备份与恢复（管理员）。
 *
 * 恢复是两步流程，刻意的：先上传体检（只看不写），把「用什么覆盖什么」摆在一屏，
 * 勾选确认后才允许提交。单步「上传即覆盖」在误选文件时没有任何补救机会。
 */
export default function BackupPanel() {
  const queryClient = useQueryClient();
  const fileRef = useRef<HTMLInputElement>(null);
  const [note, setNote] = useState("");
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [file, setFile] = useState<File | null>(null);
  const [inspection, setInspection] = useState<RestoreInspection | null>(null);
  const [confirmed, setConfirmed] = useState(false);

  const backups = useQuery({ queryKey: ["backups"], queryFn: api.backups });

  const create = useMutation({
    mutationFn: () => api.createBackup(note.trim()),
    onSuccess: (data) => {
      setError(null);
      setMessage(`已创建 ${data.name}（${formatSize(data.size)}），可在下方下载。`);
      setNote("");
      queryClient.invalidateQueries({ queryKey: ["backups"] });
    },
    onError: (err) => setError((err as Error).message),
  });

  const remove = useMutation({
    mutationFn: (name: string) => api.deleteBackup(name),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["backups"] }),
    onError: (err) => setError((err as Error).message),
  });

  const inspect = useMutation({
    mutationFn: (target: File) => api.inspectRestore(target),
    onSuccess: (data) => {
      setInspection(data);
      setConfirmed(false);
      setError(null);
    },
    onError: (err) => {
      setInspection(null);
      setError((err as Error).message);
    },
  });

  const restore = useMutation({
    mutationFn: () => api.restoreUpload(file as File, true),
    onSuccess: (data) => {
      setMessage(data.message);
      setInspection(null);
      setConfirmed(false);
      setFile(null);
      if (fileRef.current) fileRef.current.value = "";
      // 数据整体换掉了，所有列表都得重新取
      queryClient.invalidateQueries();
    },
    onError: (err) => setError((err as Error).message),
  });

  const pickFile = (selected: File | null) => {
    setFile(selected);
    setInspection(null);
    setConfirmed(false);
    setMessage(null);
    setError(null);
    if (selected) inspect.mutate(selected);
  };

  return (
    <div className="card space-y-3">
      <div className="flex items-center gap-2">
        <div className="text-sm font-medium text-slate-300">系统备份与恢复</div>
        <span className="badge bg-slate-800 text-slate-400">仅管理员</span>
      </div>
      <p className="text-xs text-slate-500">
        备份包含数据库、模型产物与签名密钥，可下载保存；恢复会<strong className="text-red-400">整体覆盖</strong>
        当前数据，并在覆盖前自动打一份预备份。
      </p>

      <div className="flex flex-wrap items-center gap-2">
        <input
          className="input w-full sm:w-56"
          placeholder="备注（可选，如「升级前」）"
          value={note}
          onChange={(event) => setNote(event.target.value)}
        />
        <button className="btn" disabled={create.isPending} onClick={() => create.mutate()}>
          {create.isPending ? "备份中…" : "立即备份"}
        </button>
        <button className="btn" onClick={() => backups.refetch()}>
          刷新
        </button>
      </div>

      {message && <p className="text-xs text-emerald-400">{message}</p>}
      {error && <p className="text-xs text-red-400">{error}</p>}

      {backups.data?.items.length ? (
        <div className="table-wrap">
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-800">
                <th className="th">时间</th>
                <th className="th">备注</th>
                <th className="th text-right">大小</th>
                <th className="th text-right">行数</th>
                <th className="th text-right">操作</th>
              </tr>
            </thead>
            <tbody>
              {backups.data.items.map((item) => (
                <tr key={item.name} className="border-t border-slate-800/60">
                  <td className="td whitespace-nowrap">{formatDateTime(item.created_at)}</td>
                  <td className="td text-slate-400">{item.error ? item.error : item.note || "—"}</td>
                  <td className="td text-right">{formatSize(item.size)}</td>
                  <td className="td text-right">{item.rows ?? "—"}</td>
                  <td className="td text-right">
                    <button
                      className="text-xs text-sky-400 hover:underline disabled:opacity-40"
                      disabled={Boolean(item.error)}
                      onClick={() => api.downloadBackup(item.name)}
                    >
                      下载
                    </button>
                    <button
                      className="ml-3 text-xs text-red-400 hover:underline"
                      onClick={() => remove.mutate(item.name)}
                    >
                      删除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="text-xs text-slate-500">还没有备份。建议每天收盘后点一次「立即备份」。</p>
      )}

      <div className="border-t border-slate-800 pt-3">
        <div className="text-sm font-medium text-slate-300">从备份恢复</div>
        <input
          ref={fileRef}
          type="file"
          accept=".tar.gz,application/gzip"
          className="mt-2 block w-full text-xs text-slate-400 file:mr-2 file:rounded file:border-0 file:bg-slate-800 file:px-3 file:py-1.5 file:text-xs file:text-slate-200"
          onChange={(event) => pickFile(event.target.files?.[0] ?? null)}
        />
        {inspect.isPending && <p className="mt-2 text-xs text-slate-500">检查归档中…</p>}

        {inspection && (
          <div className="mt-2 space-y-2">
            <pre className="whitespace-pre-wrap rounded border border-slate-800 bg-slate-950 p-2 text-xs text-slate-300">
              {inspection.summary}
            </pre>
            <div className="grid grid-cols-1 gap-2 text-xs sm:grid-cols-2">
              <div className="rounded border border-slate-800 p-2">
                <div className="text-slate-500">当前数据</div>
                <div className="text-slate-200">
                  {inspection.target.tables} 张表 / {inspection.target.rows} 行
                </div>
                <div className="text-slate-500">schema {inspection.target.schema_version || "未纳管"}</div>
              </div>
              <div className="rounded border border-slate-800 p-2">
                <div className="text-slate-500">将被替换为</div>
                <div className="text-slate-200">
                  {(inspection.manifest.database?.tables ?? []).length} 张表 /{" "}
                  {Object.values(inspection.manifest.database?.row_counts ?? {}).reduce((a, b) => a + b, 0)} 行
                </div>
                <div className="text-slate-500">
                  schema {inspection.manifest.database?.schema_version || "未纳管"}
                </div>
              </div>
            </div>
            {inspection.backup_ahead && (
              <p className="text-xs text-amber-400">
                该备份的 schema 比当前程序新，无法直接恢复：请先升级程序。
              </p>
            )}
            <label className="flex items-start gap-2 text-xs text-slate-400">
              <input
                type="checkbox"
                className="mt-0.5"
                checked={confirmed}
                onChange={(event) => setConfirmed(event.target.checked)}
              />
              <span>
                我确认用这份备份覆盖当前数据库。恢复期间会暂停定时任务，完成后建议重启服务。
              </span>
            </label>
            <button
              className="rounded-md border border-red-800 bg-red-950/40 px-3 py-1.5 text-xs text-red-300 transition hover:bg-red-900/40 disabled:opacity-40"
              disabled={!confirmed || restore.isPending || inspection.backup_ahead}
              onClick={() => restore.mutate()}
            >
              {restore.isPending ? "恢复中…" : "确认恢复"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
