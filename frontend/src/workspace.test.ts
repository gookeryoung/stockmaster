import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Workspace } from "./types";

vi.mock("./api", () => ({
  api: { workspaces: vi.fn() },
  setWorkspaceId: vi.fn(),
}));

const PERSONAL: Workspace = {
  id: 1,
  name: "我的工作区",
  description: "",
  kind: "personal",
  owner_id: 1,
  role: "owner",
  member_count: 1,
  data_total: 0,
  message_template: "",
  created_at: null,
  updated_at: null,
};

const SHARED: Workspace = { ...PERSONAL, id: 2, name: "团队", kind: "shared", role: "viewer", owner_id: 9 };

async function load() {
  const apiMod = await import("./api");
  const wsMod = await import("./workspace");
  return {
    api: apiMod.api as unknown as { workspaces: ReturnType<typeof vi.fn> },
    setWorkspaceId: apiMod.setWorkspaceId as unknown as ReturnType<typeof vi.fn>,
    store: wsMod.useWorkspace,
    canWriteWorkspace: wsMod.canWriteWorkspace,
    canManageWorkspace: wsMod.canManageWorkspace,
    ROLE_LABEL: wsMod.ROLE_LABEL,
  };
}

beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
});

describe("权限判定", () => {
  it("无工作区（隔离关闭）时按可写处理，但不能管理成员", async () => {
    const { canWriteWorkspace, canManageWorkspace } = await load();
    expect(canWriteWorkspace(null)).toBe(true);
    expect(canManageWorkspace(null)).toBe(false);
  });

  it("owner 可写可管理，viewer 均不可", async () => {
    const { canWriteWorkspace, canManageWorkspace } = await load();
    expect(canWriteWorkspace(PERSONAL)).toBe(true);
    expect(canManageWorkspace(PERSONAL)).toBe(true);
    expect(canWriteWorkspace(SHARED)).toBe(false);
    expect(canManageWorkspace(SHARED)).toBe(false);
  });

  it("editor 可写但不能管理", async () => {
    const { canWriteWorkspace, canManageWorkspace } = await load();
    const editor: Workspace = { ...PERSONAL, role: "editor" };
    expect(canWriteWorkspace(editor)).toBe(true);
    expect(canManageWorkspace(editor)).toBe(false);
  });

  it("管理员查看模式下一律只读", async () => {
    const { store, canWriteWorkspace, canManageWorkspace } = await load();
    store.setState({ list: [PERSONAL, SHARED], transient: { ...SHARED, role: "owner" } });
    expect(canWriteWorkspace({ ...SHARED, role: "owner" })).toBe(false);
    expect(canManageWorkspace({ ...SHARED, role: "owner" })).toBe(false);
  });

  it("角色中文标签齐全", async () => {
    const { ROLE_LABEL } = await load();
    expect(ROLE_LABEL).toMatchObject({ owner: "所有者", editor: "编辑者", viewer: "查看者" });
  });
});

describe("refresh", () => {
  it("保留当前选中项，并把 id 同步给 api 客户端", async () => {
    const { api, store, setWorkspaceId } = await load();
    api.workspaces.mockResolvedValue([PERSONAL, SHARED]);

    await store.getState().refresh();
    store.getState().select(2);

    expect(store.getState().list).toHaveLength(2);
    expect(store.getState().currentId).toBe(2);
    expect(setWorkspaceId).toHaveBeenCalledWith(2);
  });

  it("当前 id 不在返回列表时回落到第一个", async () => {
    const { api, store } = await load();
    api.workspaces.mockResolvedValue([PERSONAL]);
    store.setState({ currentId: 999 });

    await store.getState().refresh();

    expect(store.getState().currentId).toBe(1);
  });

  it("接口失败时退回「无工作区」，不抛出", async () => {
    const { api, store, setWorkspaceId } = await load();
    api.workspaces.mockRejectedValue(new Error("boom"));

    await expect(store.getState().refresh()).resolves.toBeUndefined();
    expect(store.getState()).toMatchObject({ list: [], currentId: null, loading: false });
    expect(setWorkspaceId).toHaveBeenCalledWith(null);
  });

  it("退出管理员查看后回到自己的个人工作区", async () => {
    const { api, store } = await load();
    const transient = { ...SHARED, id: 3, name: "他人工作区" };
    store.setState({ list: [PERSONAL, transient], currentId: 3, transient });
    api.workspaces.mockResolvedValue([PERSONAL]);

    await store.getState().exitTransient();

    expect(store.getState().transient).toBeNull();
    expect(store.getState().currentId).toBe(1);
  });

  it("reset 清空列表与选中项", async () => {
    const { store, setWorkspaceId } = await load();
    store.setState({ list: [PERSONAL], currentId: 1 });

    store.getState().reset();

    expect(store.getState().list).toEqual([]);
    expect(store.getState().currentId).toBeNull();
    expect(setWorkspaceId).toHaveBeenCalledWith(null);
  });
});
