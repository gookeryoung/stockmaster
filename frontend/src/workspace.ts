/** 当前工作区与我的角色。
 *
 * 这是「数据归属」的客户端状态：所有数据请求都会带上 `X-Workspace-Id`，所以切换
 * 工作区之后必须让各页面的查询重新执行。做法是在 Layout 里给路由容器加
 * `key={currentId}` —— 换 key 等于整棵子树重挂载，所有 `useQuery` 自然重取，
 * 比在每个页面里监听 id 变化可靠得多（漏一个页面就会出现「数据没跟着切」）。
 *
 * 隔离关闭时（`workspace_enabled=false`）这个 store 保持空列表，UI 也不显示入口，
 * 与引入工作区之前完全一致。
 */

import { create } from "zustand";
import { api, setWorkspaceId } from "./api";
import type { Workspace } from "./types";

interface WorkspaceState {
  list: Workspace[];
  currentId: number | null;
  loading: boolean;
  refresh: () => Promise<void>;
  select: (id: number) => void;
  reset: () => void;
}

export const useWorkspace = create<WorkspaceState>((set, get) => ({
  list: [],
  currentId: null,
  loading: false,

  refresh: async () => {
    set({ loading: true });
    try {
      const list = await api.workspaces();
      const current = get().currentId;
      const next =
        list.find((row) => row.id === current) ??
        list.find((row) => row.kind === "personal") ??
        list[0] ??
        null;
      set({ list, currentId: next?.id ?? null, loading: false });
      setWorkspaceId(next?.id ?? null);
    } catch {
      // 取不到列表（未登录 / 隔离关闭）时退回「无工作区」，数据请求不带该头。
      set({ list: [], currentId: null, loading: false });
      setWorkspaceId(null);
    }
  },

  select: (id) => {
    set({ currentId: id });
    setWorkspaceId(id);
  },

  reset: () => {
    set({ list: [], currentId: null, loading: false });
    setWorkspaceId(null);
  },
}));

/** 当前工作区对象（未选到时为 null）。 */
export function useCurrentWorkspace(): Workspace | null {
  return useWorkspace((state) => state.list.find((row) => row.id === state.currentId) ?? null);
}

/** 能否写数据：没有工作区（隔离关闭）时按可写处理，保持单机体验。 */
export function canWriteWorkspace(workspace: Workspace | null): boolean {
  return workspace ? workspace.role !== "viewer" : true;
}

export function canManageWorkspace(workspace: Workspace | null): boolean {
  return workspace ? workspace.role === "owner" : false;
}

export const ROLE_LABEL: Record<string, string> = {
  owner: "所有者",
  editor: "编辑者",
  viewer: "查看者",
};
