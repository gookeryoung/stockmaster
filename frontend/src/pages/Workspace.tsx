/** 工作区页：查询编排 + 分区布局。
 *
 * 页面被拆成 4 个锚点分区（概览 / 成员 / 通知 / 用量与策略），外加管理员只读总览；
 * 各分区的写操作由子组件自己持有，查询结果统一在这里拉取后下传，避免每个区块各建
 * 一份数据源。权限判断仍在服务端：这里的 canManage/canWrite 只决定控件显隐。
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { useAuth } from "../auth";
import {
  canManageWorkspace,
  canWriteWorkspace,
  ROLE_LABEL,
  useCurrentWorkspace,
  useTransientWorkspace,
  useWorkspace,
} from "../workspace";
import OverviewSection from "./workspace/OverviewSection";
import MembersSection from "./workspace/MembersSection";
import NotificationsSection from "./workspace/NotificationsSection";
import UsageSection from "./workspace/UsageSection";
import AdminOverviewSection from "./workspace/AdminOverviewSection";
import WorkspaceSectionNav from "./workspace/WorkspaceSectionNav";

export default function WorkspacePage() {
  const user = useAuth((state) => state.user);
  const config = useAuth((state) => state.config);
  const list = useWorkspace((state) => state.list);
  const loading = useWorkspace((state) => state.loading);
  const refresh = useWorkspace((state) => state.refresh);
  const transient = useTransientWorkspace();
  const exitTransient = useWorkspace((state) => state.exitTransient);
  const current = useCurrentWorkspace();

  const [message, setMessage] = useState<string | null>(null);

  const canManage = canManageWorkspace(current);
  const canWrite = canWriteWorkspace(current);
  const isAdmin = user?.role === "admin";
  const workspaceId = current?.id ?? null;
  const adminViewing = transient !== null && workspaceId !== null && transient.id === workspaceId;

  const members = useQuery({
    queryKey: ["workspace-members", workspaceId],
    queryFn: () => api.workspaceMembers(workspaceId as number),
    enabled: workspaceId !== null,
  });

  const channels = useQuery({
    queryKey: ["workspace-channels", workspaceId],
    queryFn: () => api.workspaceChannels(workspaceId as number),
    enabled: workspaceId !== null,
  });

  // 用量与配额：失败只在这张卡片里提示，不影响页面其它区块。
  const usage = useQuery({
    queryKey: ["workspace-usage", workspaceId],
    queryFn: () => api.workspaceStats(workspaceId as number),
    enabled: workspaceId !== null,
    retry: false,
  });

  // 管理员总览：后端对非管理员会退回「自己的列表」，这里只在管理员界面查询。
  const allWorkspaces = useQuery({
    queryKey: ["workspaces-all"],
    queryFn: () => api.workspaces(true),
    enabled: isAdmin && Boolean(config?.workspace_enabled),
    retry: false,
  });

  // 用量趋势：每日快照；少于 2 个点时由 UsageSection 降级为提示文案。
  const usageHistory = useQuery({
    queryKey: ["workspace-usage-history", workspaceId],
    queryFn: () => api.workspaceUsageHistory(workspaceId as number),
    enabled: workspaceId !== null,
    retry: false,
  });

  const flash = (text: string) => setMessage(text);

  if (!config?.workspace_enabled) {
    return (
      <div className="card text-sm text-slate-400">
        当前未启用数据隔离（SM_DATA_ISOLATION=false），所有人共享同一份数据，因此没有工作区概念。
      </div>
    );
  }

  if (!user) {
    return <div className="card text-sm text-slate-400">请先登录后再管理工作区。</div>;
  }

  if (!current) {
    return (
      <div className="card flex flex-wrap items-center gap-2 text-sm text-slate-400">
        <span className="flex-1">
          {loading ? "工作区加载中…" : "未找到可用工作区。请刷新页面重试，或联系管理员检查工作区配置。"}
        </span>
        {!loading && (
          <button className="btn px-2 py-1 text-xs" onClick={() => void refresh()}>
            重新加载
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {message && (
        <div
          role="status"
          aria-live="polite"
          className="flex items-start gap-2 rounded-md border border-slate-700 bg-slate-900 px-3 py-2 text-xs text-slate-300"
        >
          <span className="flex-1">{message}</span>
          <button className="text-slate-500 hover:text-slate-300" onClick={() => setMessage(null)}>
            关闭
          </button>
        </div>
      )}

      {!canWrite && !adminViewing && (
        <div className="rounded-md border border-amber-800/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-300">
          你在「{current.name}」是查看者：可以阅读这里的自选、预警与回测，但不能修改。
        </div>
      )}

      {adminViewing && (
        <div className="flex flex-wrap items-center gap-2 rounded-md border border-amber-800/60 bg-amber-950/30 px-3 py-2 text-xs text-amber-300">
          <span className="flex-1">
            管理员查看模式：你不是「{current.name}」的成员，仅用于排障/支持。此页面一律只读，
            写操作已禁用；退出查看后回到你的个人工作区。
          </span>
          <button
            className="btn border-amber-700 px-2 py-1 text-xs text-amber-200 hover:border-amber-500"
            disabled={loading}
            onClick={() => void exitTransient()}
          >
            退出查看
          </button>
        </div>
      )}

      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-lg font-semibold text-slate-100">工作区设置 · {current.name}</h1>
        <p className="text-xs text-slate-500">
          {adminViewing ? "管理员查看" : `当前角色 ${ROLE_LABEL[current.role] ?? current.role}`} · 共{" "}
          {list.length} 个工作区 · 切换工作区后全站数据随之切换
        </p>
      </div>

      <WorkspaceSectionNav />

      <OverviewSection
        current={current}
        list={list}
        currentId={workspaceId}
        members={members.data}
        membersLoading={members.isLoading}
        canManage={canManage}
        adminViewing={adminViewing}
        onFlash={flash}
      />

      {isAdmin && (
        <AdminOverviewSection
          workspaces={allWorkspaces.data}
          loading={allWorkspaces.isLoading}
          error={allWorkspaces.isError}
          onRetry={() => void allWorkspaces.refetch()}
        />
      )}

      <MembersSection
        current={current}
        members={members.data}
        membersLoading={members.isLoading}
        membersError={members.isError}
        onRetryMembers={() => void members.refetch()}
        canManage={canManage}
        adminViewing={adminViewing}
        onFlash={flash}
      />

      <NotificationsSection
        current={current}
        channels={channels.data}
        channelsLoading={channels.isLoading}
        channelsError={channels.isError}
        onRetryChannels={() => void channels.refetch()}
        canManage={canManage}
        adminViewing={adminViewing}
        onFlash={flash}
      />

      <UsageSection
        current={current}
        usage={usage.data}
        usageLoading={usage.isLoading}
        usageError={usage.isError}
        onRetryUsage={() => void usage.refetch()}
        history={usageHistory.data}
        historyLoading={usageHistory.isLoading}
        historyError={usageHistory.isError}
        onRetryHistory={() => void usageHistory.refetch()}
        canManage={canManage}
        adminViewing={adminViewing}
        onFlash={flash}
      />
    </div>
  );
}
