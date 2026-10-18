import { beforeEach, describe, expect, it, vi } from "vitest";
import type { AuthConfig, AuthUser } from "./types";

/** token 由 api 客户端持有；这里用可变持有量代替真实的 localStorage 实现。 */
const session = vi.hoisted(() => ({ token: null as string | null }));

vi.mock("./api", () => ({
  api: {
    authConfig: vi.fn(),
    me: vi.fn(),
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  },
  authEvents: new EventTarget(),
  getAuthToken: () => session.token,
  setAuthToken: (next: string | null) => {
    session.token = next;
  },
}));

const CONFIG: AuthConfig = {
  auth_enabled: true,
  allow_register: true,
  has_users: true,
  wechat_enabled: false,
  wechat_mode: "open",
  workspace_enabled: true,
  version: "0.3.0",
} as AuthConfig;

const USER = { id: 1, username: "alice", role: "admin", status: "active" } as AuthUser;

async function load() {
  const apiMod = await import("./api");
  const authMod = await import("./auth");
  return {
    api: apiMod.api as unknown as {
      authConfig: ReturnType<typeof vi.fn>;
      me: ReturnType<typeof vi.fn>;
      login: ReturnType<typeof vi.fn>;
      register: ReturnType<typeof vi.fn>;
      logout: ReturnType<typeof vi.fn>;
    },
    authEvents: apiMod.authEvents,
    useAuth: authMod.useAuth,
    canManageUsers: authMod.canManageUsers,
    requiresLogin: authMod.requiresLogin,
  };
}

beforeEach(() => {
  vi.resetModules();
  session.token = null;
});

describe("init", () => {
  it("没有 token 时只拉配置，user 保持 null", async () => {
    const { api, useAuth } = await load();
    api.authConfig.mockResolvedValue(CONFIG);

    await useAuth.getState().init();

    expect(api.authConfig).toHaveBeenCalledTimes(1);
    expect(api.me).not.toHaveBeenCalled();
    expect(useAuth.getState()).toMatchObject({ ready: true, config: CONFIG, user: null });
  });

  it("有 token 时恢复会话", async () => {
    const { api, useAuth } = await load();
    session.token = "tok";
    api.authConfig.mockResolvedValue(CONFIG);
    api.me.mockResolvedValue(USER);

    await useAuth.getState().init();

    expect(useAuth.getState().user).toEqual(USER);
  });

  it("token 失效时清掉 token，不阻塞启动", async () => {
    const { api, useAuth } = await load();
    session.token = "expired";
    api.authConfig.mockResolvedValue(CONFIG);
    api.me.mockRejectedValue(new Error("401"));

    await useAuth.getState().init();

    expect(useAuth.getState()).toMatchObject({ ready: true, user: null });
    expect(session.token).toBeNull();
  });

  it("配置接口失败也能进入 ready 状态（离线可用的工作台）", async () => {
    const { api, useAuth } = await load();
    api.authConfig.mockRejectedValue(new Error("network"));

    await useAuth.getState().init();

    expect(useAuth.getState()).toMatchObject({ ready: true, config: null });
  });

  it("并发调用 init 只发一次请求（StrictMode 双挂载）", async () => {
    const { api, useAuth } = await load();
    api.authConfig.mockResolvedValue(CONFIG);

    await Promise.all([useAuth.getState().init(), useAuth.getState().init(), useAuth.getState().init()]);

    expect(api.authConfig).toHaveBeenCalledTimes(1);
  });
});

describe("登录态", () => {
  it("login 保存 token 与用户", async () => {
    const { api, useAuth } = await load();
    api.login.mockResolvedValue({ token: "tok-2", user: USER });

    await useAuth.getState().login("alice", "pw");

    expect(api.login).toHaveBeenCalledWith("alice", "pw");
    expect(session.token).toBe("tok-2");
    expect(useAuth.getState().user).toEqual(USER);
  });

  it("register 同样落地会话", async () => {
    const { api, useAuth } = await load();
    api.register.mockResolvedValue({ token: "tok-3", user: USER });

    await useAuth.getState().register({ username: "bob", password: "pw123456" });

    expect(session.token).toBe("tok-3");
  });

  it("login 失败时不改本地状态", async () => {
    const { api, useAuth } = await load();
    api.login.mockRejectedValue(new Error("用户名或密码错误"));

    await expect(useAuth.getState().login("alice", "bad")).rejects.toThrow("用户名或密码错误");
    expect(session.token).toBeNull();
    expect(useAuth.getState().user).toBeNull();
  });

  it("logout 即使接口失败也清除本地会话", async () => {
    const { api, useAuth } = await load();
    api.logout.mockRejectedValue(new Error("500"));
    session.token = "tok";
    useAuth.setState({ user: USER });

    await useAuth.getState().logout();

    expect(session.token).toBeNull();
    expect(useAuth.getState().user).toBeNull();
  });

  it("refreshUser 无 token 时直接置空", async () => {
    const { api, useAuth } = await load();
    useAuth.setState({ user: USER });

    await useAuth.getState().refreshUser();

    expect(api.me).not.toHaveBeenCalled();
    expect(useAuth.getState().user).toBeNull();
  });

  it("401 广播会踢掉当前用户", async () => {
    const { authEvents, useAuth } = await load();
    useAuth.setState({ user: USER });

    authEvents.dispatchEvent(new Event("unauthorized"));

    expect(useAuth.getState().user).toBeNull();
  });
});

describe("权限降级", () => {
  it("canManageUsers: 管理员为真，普通用户为假", async () => {
    const { canManageUsers } = await load();
    expect(canManageUsers(CONFIG, USER)).toBe(true);
    expect(canManageUsers(CONFIG, { ...USER, role: "user" } as AuthUser)).toBe(false);
    expect(canManageUsers(null, USER)).toBe(true);
  });

  it("canManageUsers: 未登录时仅在关闭鉴权的情况下为真（本机模式）", async () => {
    const { canManageUsers } = await load();
    expect(canManageUsers(CONFIG, null)).toBe(false);
    expect(canManageUsers({ ...CONFIG, auth_enabled: false }, null)).toBe(true);
    expect(canManageUsers(null, null)).toBe(false);
  });

  it("requiresLogin: 开启鉴权且无用户时才拦截", async () => {
    const { requiresLogin } = await load();
    expect(requiresLogin(CONFIG, null)).toBe(true);
    expect(requiresLogin(CONFIG, USER)).toBe(false);
    expect(requiresLogin({ ...CONFIG, auth_enabled: false }, null)).toBe(false);
    expect(requiresLogin(null, null)).toBe(false);
  });
});
