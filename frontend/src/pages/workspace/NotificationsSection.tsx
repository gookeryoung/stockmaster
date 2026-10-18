/** 通知通道 + 预警消息模板。
 *
 * 通道是**数据出口**：读对所有成员开放（地址已脱敏），写操作只出现在 owner 的
 * 界面上，真正校验在服务端。编辑时 API 只回显掩码，所以“改地址”必须显式勾选；
 * 不勾选时 PATCH 省略 url，由后端保持原值。
 */

import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api";
import ConfirmDialog from "../../components/ConfirmDialog";
import { useWorkspace } from "../../workspace";
import type { Workspace, WorkspaceChannel, WorkspaceChannelKind } from "../../types";

const CHANNEL_KIND_LABEL: Record<WorkspaceChannelKind, string> = {
  webhook: "Webhook",
  bark: "Bark",
  email: "邮件",
};

const TEMPLATE_FIELDS = ["title", "body", "code", "rule_id", "level"] as const;

const TEMPLATE_SAMPLE: Record<string, string> = {
  title: "放量突破",
  body: "600519 成交量较前五日均值放大 2.1 倍",
  code: "600519",
  rule_id: "12",
  level: "warning",
};

/** 本地预览：与后端 `str.format` 的占位符白名单保持一致，非法模板提示原因。 */
function renderTemplatePreview(template: string): { ok: boolean; text: string } {
  const value = template.trim();
  if (!value) {
    return { ok: true, text: "默认正文示例：【预警】放量突破 - 600519" };
  }
  for (const match of value.matchAll(/\{([^{}]*)\}/g)) {
    const raw = match[1];
    const name = raw.split(":")[0].split("!")[0].trim();
    if (!(TEMPLATE_FIELDS as readonly string[]).includes(name)) {
      return { ok: false, text: `不支持占位符 {${name}}，只允许 ${TEMPLATE_FIELDS.map((f) => `{${f}}`).join(" ")}` };
    }
  }
  const rest = value.replace(/\{[^{}]*\}/g, "");
  if (rest.includes("{") || rest.includes("}")) {
    return { ok: false, text: "花括号未闭合，保存会被服务端以 422 拒绝" };
  }
  const text = value.replace(/\{([^{}]*)\}/g, (_match, raw: string) => {
    const name = raw.split(":")[0].split("!")[0].trim();
    return TEMPLATE_SAMPLE[name] ?? "";
  });
  return { ok: true, text };
}

function channelUrlPlaceholder(kind: WorkspaceChannelKind): string {
  if (kind === "email") return "ops@example.com, team@example.com";
  if (kind === "bark") return "https://api.day.app/你的Key";
  return "https://oapi.dingtalk.com/robot/send?access_token=...";
}

/** 后端 send_test_detail 的失败原因 → 可执行的下一步。 */
const TEST_REASON_HINT: Record<string, string> = {
  smtp_not_configured: "部署未配置 SMTP（SM_SMTP_HOST 为空），邮件通道不会投递。",
  no_recipients: "收件人为空，请编辑通道填写收件人。",
  delivery_failed: "地址或网络不可达，请检查 Webhook / Bark 地址与网络。",
  unknown_channel: "通道类型不受支持，请重新创建通道。",
  module_unavailable: "服务端投递模块不可用，请检查部署日志。",
  exception: "投递过程异常（地址格式或网络错误），请检查配置后重试。",
};

interface Props {
  current: Workspace;
  channels: WorkspaceChannel[] | undefined;
  channelsLoading: boolean;
  channelsError: boolean;
  onRetryChannels: () => void;
  canManage: boolean;
  adminViewing?: boolean;
  onFlash: (text: string) => void;
}

export default function NotificationsSection({
  current,
  channels,
  channelsLoading,
  channelsError,
  onRetryChannels,
  canManage,
  adminViewing = false,
  onFlash,
}: Props) {
  const queryClient = useQueryClient();
  const refresh = useWorkspace((state) => state.refresh);

  // 添加通道
  const [channelName, setChannelName] = useState("");
  const [channelKind, setChannelKind] = useState<WorkspaceChannelKind>("webhook");
  const [channelUrl, setChannelUrl] = useState("");
  const [channelEnabled, setChannelEnabled] = useState(true);

  // 编辑通道（掩码回显 + 显式勾选“修改地址”）
  const [editing, setEditing] = useState<WorkspaceChannel | null>(null);
  const [editName, setEditName] = useState("");
  const [editKind, setEditKind] = useState<WorkspaceChannelKind>("webhook");
  const [editEnabled, setEditEnabled] = useState(true);
  const [editModifyUrl, setEditModifyUrl] = useState(false);
  const [editUrl, setEditUrl] = useState("");

  // 测试结果用行内文案反馈，不弹 alert：delivered=false 是预期结果而非系统错误。
  const [testResult, setTestResult] = useState<{
    channel: string;
    delivered: boolean;
    reason?: string;
  } | null>(null);

  // 删除通道 / 恢复默认模板：统一确认弹层。
  const [deleteTarget, setDeleteTarget] = useState<WorkspaceChannel | null>(null);
  const [resetTemplateOpen, setResetTemplateOpen] = useState(false);

  // 消息模板
  const [messageTemplate, setMessageTemplate] = useState("");
  useEffect(() => {
    setMessageTemplate(current.message_template ?? "");
  }, [current.id, current.message_template]);
  const preview = renderTemplatePreview(messageTemplate);

  const invalidateChannels = () => {
    queryClient.invalidateQueries({ queryKey: ["workspace-channels", current.id] });
    queryClient.invalidateQueries({ queryKey: ["workspace-usage", current.id] });
  };

  const createChannel = useMutation({
    mutationFn: () =>
      api.createWorkspaceChannel(current.id, {
        name: channelName.trim(),
        kind: channelKind,
        url: channelUrl.trim(),
        enabled: channelEnabled,
      }),
    onSuccess: (channel) => {
      setChannelName("");
      setChannelUrl("");
      setChannelEnabled(true);
      onFlash(`已添加通道「${channel.name}」`);
      invalidateChannels();
    },
    onError: (error) => onFlash(`添加通道失败：${(error as Error).message}`),
  });

  const updateChannel = useMutation({
    mutationFn: ({
      channelId,
      payload,
    }: {
      channelId: number;
      payload: { name?: string; kind?: WorkspaceChannelKind; url?: string; enabled?: boolean };
    }) => api.updateWorkspaceChannel(current.id, channelId, payload),
    onSuccess: (channel) => {
      onFlash(channel.enabled ? `已启用通道「${channel.name}」` : `已停用通道「${channel.name}」`);
      setEditing(null);
      invalidateChannels();
    },
    onError: (error) => onFlash(`更新通道失败：${(error as Error).message}`),
  });

  const deleteChannel = useMutation({
    mutationFn: (channelId: number) => api.deleteWorkspaceChannel(current.id, channelId),
    onSuccess: () => {
      setDeleteTarget(null);
      onFlash("已删除通道");
      invalidateChannels();
    },
    onError: (error) => {
      setDeleteTarget(null);
      onFlash(`删除通道失败：${(error as Error).message}`);
    },
  });

  const testChannel = useMutation({
    mutationFn: (channel: WorkspaceChannel) => api.testWorkspaceChannel(current.id, channel.id),
    onSuccess: (result) => {
      setTestResult({ channel: result.channel, delivered: result.delivered, reason: result.reason });
    },
    onError: (error) => onFlash(`测试失败：${(error as Error).message}`),
  });

  const saveTemplate = useMutation({
    mutationFn: (template: string) =>
      api.updateWorkspace(current.id, { message_template: template }),
    onSuccess: async (updated, template) => {
      setResetTemplateOpen(false);
      onFlash(template ? "已保存预警消息模板" : "已恢复默认消息模板");
      setMessageTemplate(updated.message_template ?? "");
      await refresh();
    },
    onError: (error, template) => {
      setResetTemplateOpen(false);
      // 恢复默认失败时把编辑框还原为服务端现值，避免界面看似已清空、实际未保存。
      if (!template) setMessageTemplate(current.message_template ?? "");
      onFlash(`保存消息模板失败：${(error as Error).message}`);
    },
  });

  const startEdit = (channel: WorkspaceChannel) => {
    setEditing(channel);
    setEditName(channel.name);
    setEditKind(channel.kind);
    setEditEnabled(channel.enabled);
    setEditModifyUrl(false);
    setEditUrl("");
  };

  const submitEdit = () => {
    if (!editing) return;
    const kindChanged = editKind !== editing.kind;
    const mustProvideUrl = editModifyUrl || kindChanged;
    if (mustProvideUrl && !editUrl.trim()) {
      onFlash("改变通道类型或地址时必须填写新的地址");
      return;
    }
    updateChannel.mutate({
      channelId: editing.id,
      payload: {
        name: editName.trim(),
        kind: editKind,
        enabled: editEnabled,
        ...(mustProvideUrl ? { url: editUrl.trim() } : {}),
      },
    });
  };

  return (
    <section id="notifications" className="scroll-mt-16 space-y-3">
      <div className="card">
        <div className="mb-2">
          <h2 className="text-sm font-semibold text-slate-100">通知通道</h2>
          <p className="mt-1 text-xs text-slate-500">
            通道只接收当前工作区的预警；地址保存后仅以脱敏形式展示。
            {adminViewing
              ? "管理员查看模式下通道列表只读。"
              : canManage
                ? "启用、编辑、测试与删除由你管理。"
                : "只有所有者可以增删改通道或发送测试通知。"}
          </p>
        </div>

        <div className="table-wrap">
          <table className="w-full">
            <thead>
              <tr className="border-b border-slate-800">
                <th className="th">名称</th>
                <th className="th">类型</th>
                <th className="th">地址（脱敏）</th>
                <th className="th">状态</th>
                {canManage && <th className="th" />}
              </tr>
            </thead>
            <tbody>
              {channelsLoading && (
                <tr>
                  <td className="td text-slate-500" colSpan={canManage ? 5 : 4}>
                    加载中…
                  </td>
                </tr>
              )}
              {!channelsLoading && channelsError && (
                <tr>
                  <td className="td text-xs text-amber-300" colSpan={canManage ? 5 : 4}>
                    通道加载失败{" "}
                    <button className="underline" type="button" onClick={onRetryChannels}>
                      重试
                    </button>
                  </td>
                </tr>
              )}
              {!channelsLoading &&
                !channelsError &&
                (channels ?? []).map((row) => (
                  <tr key={row.id} className="border-b border-slate-800/50 last:border-0">
                    <td className="td font-medium text-slate-100">{row.name}</td>
                    <td className="td text-slate-400">{CHANNEL_KIND_LABEL[row.kind] ?? row.kind}</td>
                    <td className="td font-mono text-xs text-slate-500">{row.url || "—"}</td>
                    <td className="td">
                      <span
                        className={`badge ${row.enabled ? "bg-emerald-900/50 text-emerald-300" : "bg-slate-800 text-slate-400"}`}
                      >
                        {row.enabled ? "启用" : "停用"}
                      </span>
                    </td>
                    {canManage && (
                      <td className="td text-right">
                        <div className="flex justify-end gap-1">
                          <button
                            className="btn px-2 py-1 text-xs"
                            disabled={updateChannel.isPending}
                            onClick={() =>
                              updateChannel.mutate({ channelId: row.id, payload: { enabled: !row.enabled } })
                            }
                          >
                            {row.enabled ? "停用" : "启用"}
                          </button>
                          <button className="btn px-2 py-1 text-xs" onClick={() => startEdit(row)}>
                            编辑
                          </button>
                          <button
                            className="btn px-2 py-1 text-xs"
                            disabled={testChannel.isPending}
                            onClick={() => testChannel.mutate(row)}
                          >
                            测试
                          </button>
                          <button
                            className="btn px-2 py-1 text-xs"
                            disabled={deleteChannel.isPending}
                            onClick={() => setDeleteTarget(row)}
                          >
                            删除
                          </button>
                        </div>
                      </td>
                    )}
                  </tr>
                ))}
              {!channelsLoading && !channelsError && (channels ?? []).length === 0 && (
                <tr>
                  <td className="td text-slate-500" colSpan={canManage ? 5 : 4}>
                    尚未配置通道，本工作区触发预警时不会外发。
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {testResult && (
          <p
            role="status"
            aria-live="polite"
            className={`mt-2 text-xs ${testResult.delivered ? "text-emerald-300" : "text-amber-300"}`}
          >
            {testResult.delivered
              ? `测试结果：已送达（${testResult.channel}）`
              : `测试结果：未送达（${testResult.channel}）${
                  testResult.reason
                    ? `——${TEST_REASON_HINT[testResult.reason] ?? `原因：${testResult.reason}。`}`
                    : "——请检查地址、网络或 SMTP 配置。"
                }通道仍已保存，不影响规则。`}
          </p>
        )}

        {canManage && editing && (
          <form
            className="mt-3 flex flex-wrap items-end gap-2 rounded-md border border-slate-800 bg-slate-950/40 p-3"
            onSubmit={(event) => {
              event.preventDefault();
              submitEdit();
            }}
          >
            <div>
              <label className="label" htmlFor="edit-channel-name">
                名称*
              </label>
              <input
                id="edit-channel-name"
                className="input w-40"
                value={editName}
                onChange={(event) => setEditName(event.target.value)}
                maxLength={64}
              />
            </div>
            <div>
              <label className="label" htmlFor="edit-channel-kind">
                类型*
              </label>
              <select
                id="edit-channel-kind"
                className="input"
                value={editKind}
                onChange={(event) => setEditKind(event.target.value as WorkspaceChannelKind)}
              >
                <option value="webhook">Webhook</option>
                <option value="bark">Bark</option>
                <option value="email">邮件（SMTP）</option>
              </select>
            </div>
            <div className="min-w-[20rem] flex-1">
              <label className="label" htmlFor="edit-channel-url">
                地址{editModifyUrl || editKind !== editing.kind ? "*" : "（保持原值）"}
              </label>
              <input
                id="edit-channel-url"
                className="input w-full font-mono text-xs"
                placeholder={editModifyUrl ? channelUrlPlaceholder(editKind) : editing.url}
                value={editUrl}
                disabled={!editModifyUrl && editKind === editing.kind}
                onChange={(event) => setEditUrl(event.target.value)}
                maxLength={512}
              />
            </div>
            <label className="flex items-center gap-2 pb-2 text-xs text-slate-300">
              <input
                type="checkbox"
                checked={editModifyUrl}
                onChange={(event) => {
                  setEditModifyUrl(event.target.checked);
                  if (!event.target.checked) setEditUrl("");
                }}
              />
              修改地址
            </label>
            <label className="flex items-center gap-2 pb-2 text-xs text-slate-300">
              <input
                type="checkbox"
                checked={editEnabled}
                onChange={(event) => setEditEnabled(event.target.checked)}
              />
              启用
            </label>
            <button className="btn btn-primary" disabled={!editName.trim() || updateChannel.isPending}>
              {updateChannel.isPending ? "保存中…" : "保存通道"}
            </button>
            <button className="btn" type="button" onClick={() => setEditing(null)}>
              取消
            </button>
          </form>
        )}

        {canManage && !editing && (
          <form
            className="mt-3 flex flex-wrap items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (channelName.trim() && channelUrl.trim()) createChannel.mutate();
            }}
          >
            <div>
              <label className="label" htmlFor="channel-name">
                名称*
              </label>
              <input
                id="channel-name"
                className="input w-40"
                placeholder="例如：投研群机器人"
                value={channelName}
                onChange={(event) => setChannelName(event.target.value)}
                maxLength={64}
              />
            </div>
            <div>
              <label className="label" htmlFor="channel-kind">
                类型*
              </label>
              <select
                id="channel-kind"
                className="input"
                value={channelKind}
                onChange={(event) => setChannelKind(event.target.value as WorkspaceChannelKind)}
              >
                <option value="webhook">Webhook</option>
                <option value="bark">Bark</option>
                <option value="email">邮件（SMTP）</option>
              </select>
            </div>
            <div>
              <label className="label" htmlFor="channel-url">
                {channelKind === "email" ? "收件人（逗号分隔）*" : "地址*"}
              </label>
              <input
                id="channel-url"
                className={`input w-96 ${channelKind === "email" ? "" : "font-mono text-xs"}`}
                placeholder={channelUrlPlaceholder(channelKind)}
                value={channelUrl}
                onChange={(event) => setChannelUrl(event.target.value)}
                maxLength={512}
              />
            </div>
            <label className="flex items-center gap-2 pb-2 text-sm text-slate-300">
              <input
                type="checkbox"
                checked={channelEnabled}
                onChange={(event) => setChannelEnabled(event.target.checked)}
              />
              启用
            </label>
            <button
              className="btn btn-primary"
              disabled={!channelName.trim() || !channelUrl.trim() || createChannel.isPending}
            >
              {createChannel.isPending ? "添加中…" : "添加通道"}
            </button>
          </form>
        )}
      </div>

      <div className="card">
        <h2 className="text-sm font-semibold text-slate-100">预警消息模板</h2>
        <p className="mt-1 text-xs text-slate-500">
          占位符：<code>{"{title} {body} {code} {rule_id} {level}"}</code>；只作用于正文，标题保持原样；
          留空 = 使用系统默认正文。模板变更只影响之后的外发消息，历史触发记录不重写。
        </p>
        <textarea
          className="input mt-2 min-h-[76px] w-full font-mono text-xs"
          value={messageTemplate}
          onChange={(event) => setMessageTemplate(event.target.value)}
          maxLength={2000}
          readOnly={!canManage}
          aria-label="预警消息模板"
          placeholder="留空使用默认标题/正文，例如：【{level}】{title} - {code}"
        />
        <p className={`mt-1 text-[11px] ${preview.ok ? "text-slate-500" : "text-amber-300"}`} role="status">
          预览：{preview.text}
        </p>
        {canManage && (
          <div className="mt-2 flex gap-2">
            <button
              className="btn btn-primary"
              disabled={saveTemplate.isPending}
              onClick={() => saveTemplate.mutate(messageTemplate)}
            >
              {saveTemplate.isPending ? "保存中…" : "保存模板"}
            </button>
            <button
              className="btn"
              type="button"
              disabled={saveTemplate.isPending}
              onClick={() => setResetTemplateOpen(true)}
            >
              恢复默认
            </button>
          </div>
        )}
        {adminViewing && (
          <p className="mt-2 text-xs text-amber-300/90">管理员查看模式下模板只读，不能保存或恢复默认。</p>
        )}
      </div>

      <ConfirmDialog
        open={deleteTarget !== null}
        title={`删除通知通道「${deleteTarget?.name ?? ""}」`}
        description={
          <>
            删除后本工作区将不再向该地址发送预警；已发出的通知不受影响。该操作只删除通道，
            不会删除任何预警规则或历史触发记录。
          </>
        }
        confirmLabel="删除通道"
        danger
        busy={deleteChannel.isPending}
        onCancel={() => setDeleteTarget(null)}
        onConfirm={() => deleteTarget && deleteChannel.mutate(deleteTarget.id)}
      />

      <ConfirmDialog
        open={resetTemplateOpen}
        title="恢复默认消息模板"
        description={
          <>
            当前自定义模板将被清空，之后的外发通知使用系统默认正文；
            <strong className="text-slate-200">历史触发记录与已发消息不会重写</strong>。
          </>
        }
        confirmLabel="清空模板并恢复默认"
        danger
        busy={saveTemplate.isPending}
        onCancel={() => setResetTemplateOpen(false)}
        onConfirm={() => {
          setMessageTemplate("");
          saveTemplate.mutate("");
        }}
      />
    </section>
  );
}
