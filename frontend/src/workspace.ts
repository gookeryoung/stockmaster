/** 当前工作区与我的角色。
 *
 * 这是「数据归属」的客户端状态：所有数据请求都会带上 `X-Workspace-Id`，所以切换
 * 工作区之后必须让各页面的查询重新执行。做法是在 Layout 里给路由容器加
 * `key={currentId}` —— 换 key 等于整棵子树重挂载，所有 `useQuery` 自然重取，
 * 比在每个页面里监听 id 变化可靠得多（漏一个页面就会出现「数据没跟着切」）。
 *
 * 管理员跨区查看（transient）
 * --------------------------
 * 管理员可以从「全部工作区」总览进入一个**自己不是成员**的工作区排障。这种查看是
 * 临时的：该工作区会被并入 `list` 并标记在 `transient`，退出查看或切换到普通工作区
 * 时移除。查看模式下所有写能力按只读处理（见 canWriteWorkspace/canManageWorkspace），
 * 即使后端对管理员放行写操作，UI 也不提供写入口。
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
  /** 管理员跨区查看的临时工作区；非 null 表示处于“查看模式”。 */
  transient: Workspace | null;
  loading: boolean;
  refresh: () => Promise<void>;
  select: (id: number) => void;
  /** 管理员进入非成员工作区只读查看。 */
  selectTransient: (workspace: Workspace) => void;
  /** 退出管理员查看：回到自己的个人工作区。 */
  exitTransient: () => Promise<void>;
  reset: () => void;
}

function withoutTransient(list: Workspace[], transient: Workspace | null): Workspace[] {
  return transient ? list.filter((row) => row.id !== transient.id) : list;
}

export const useWorkspace = create<WorkspaceState>((set, get) => ({
  list: [],
  currentId: null,
  transient: null,
  loading: false,

  refresh: async () => {
    set({ loading: true });
    try {
      const fetched = await api.workspaces();
      const { currentId, transient } = get();
      let nextList = fetched;
      let next = fetched.find((row) => row.id === currentId) ?? null;

      // 查看模式：当前工作区不在“我参与的工作区”里，但 transient 还记着它，保留。
      if (!next && transient && transient.id === currentId) {
        nextList = [...fetched, transient];
        next = transient;
      }
      if (!next) {
        next = nextList.find((row) => row.kind === "personal") ?? nextList[0] ?? null;
      }
      const keptTransient = transient && next?.id === transient.id ? transient : null;
      set({ list: nextList, currentId: next?.id ?? null, transient: keptTransient, loading: false });
      setWorkspaceId(next?.id ?? null);
    } catch {
      // 取不到列表（未登录 / 隔离关闭）时退回「无工作区」，数据请求不带该头。
      set({ list: [], currentId: null, transient: null, loading: false });
      setWorkspaceId(null);
    }
  },

  select: (id) => {
    const { transient, list } = get();
    if (transient && transient.id === id) {
      // 在切换器里重新选中当前查看的工作区：保持查看模式。
      set({ currentId: id });
      setWorkspaceId(id);
      return;
    }
    set({ currentId: id, transient: null, list: withoutTransient(list, transient) });
    setWorkspaceId(id);
  },

  selectTransient: (workspace) => {
    const { list, transient } = get();
    // 角色按 owner 展示（管理员在服务端被当作 owner 放行），写能力由
    // canWriteWorkspace/canManageWorkspace 按 transient 强制关闭。
    const viewing: Workspace = { ...workspace, role: "owner" };
    set({
      list: [...withoutTransient(list, transient), viewing],
      currentId: viewing.id,
      transient: viewing,
    });
    setWorkspaceId(viewing.id);
  },

  exitTransient: async () => {
    const { list, transient } = get();
    set({ transient: null, list: withoutTransient(list, transient) });
    // currentId 仍指向临时工作区，refresh 会回退到自己的个人工作区。
    await get().refresh();
  },

  reset: () => {
    set({ list: [], currentId: null, transient: null, loading: false });
    setWorkspaceId(null);
  },
}));

/** 当前工作区对象（未选到时为 null）。查看模式下返回的是 transient 对象。 */
export function useCurrentWorkspace(): Workspace | null {
  return useWorkspace((state) => state.list.find((row) => row.id === state.currentId) ?? null);
}

/** 管理员查看模式下的临时工作区；普通模式为 null。 */
export function useTransientWorkspace(): Workspace | null {
  return useWorkspace((state) => state.transient);
}

function isTransientWorkspace(workspace: Workspace | null): boolean {
  if (!workspace) return false;
  const transient = useWorkspace.getState().transient;
  return transient !== null && transient.id === workspace.id;
}

/** 能否写数据：没有工作区（隔离关闭）时按可写处理，保持单机体验；查看模式恒只读。 */
export function canWriteWorkspace(workspace: Workspace | null): boolean {
  if (!workspace) return true;
  if (isTransientWorkspace(workspace)) return false;
  return workspace.role !== "viewer";
}

export function canManageWorkspace(workspace: Workspace | null): boolean {
  if (!workspace) return false;
  if (isTransientWorkspace(workspace)) return false;
  return workspace.role === "owner";
}

export const ROLE_LABEL: Record<string, string> = {
  owner: "所有者",
  editor: "编辑者",
  viewer: "查看者",
};
