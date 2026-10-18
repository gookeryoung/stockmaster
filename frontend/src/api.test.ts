import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { fetchMock, jsonResponse } from "./test/utils";

type ApiModule = typeof import("./api");

/** 每个用例重新加载模块：token / workspace 是模块级状态，复用会互相污染。 */
async function loadApi(): Promise<ApiModule> {
  return import("./api");
}

beforeEach(() => {
  vi.resetModules();
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("workspace id 状态", () => {
  it("非法值一律读成 null", async () => {
    localStorage.setItem("sm.workspace.id", "abc");
    const api = await loadApi();
    expect(api.getWorkspaceId()).toBeNull();

    api.setWorkspaceId(0);
    expect(api.getWorkspaceId()).toBeNull();

    api.setWorkspaceId(-3);
    expect(api.getWorkspaceId()).toBeNull();

    api.setWorkspaceId(7);
    expect(api.getWorkspaceId()).toBe(7);
    expect(localStorage.getItem("sm.workspace.id")).toBe("7");
  });
});

describe("request", () => {
  it("带上 Content-Type 与 JSON body", async () => {
    const { fn, calls } = fetchMock(jsonResponse({ body: { token: "t", user: { id: 1 } } }));
    vi.stubGlobal("fetch", fn);
    const { api } = await loadApi();

    await api.login("alice", "secret123");

    expect(calls[0].url).toBe("/api/v1/auth/login");
    expect(calls[0].init.method).toBe("POST");
    expect(calls[0].init.body).toBe(JSON.stringify({ username: "alice", password: "secret123" }));
    expect((calls[0].init.headers as Record<string, string>)["Content-Type"]).toBe("application/json");
  });

  it("登录后请求带 Authorization，切工作区后带 X-Workspace-Id", async () => {
    const { fn, calls } = fetchMock(jsonResponse({ body: {} }), jsonResponse({ body: {} }));
    vi.stubGlobal("fetch", fn);
    const { api, setAuthToken, setWorkspaceId } = await loadApi();

    setAuthToken("tok-1");
    setWorkspaceId(42);
    await api.health();
    await api.health();

    const headers = calls[1].init.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer tok-1");
    expect(headers["X-Workspace-Id"]).toBe("42");
  });

  it("204 返回 undefined 而不是解析 JSON", async () => {
    const { fn } = fetchMock(jsonResponse({ status: 204 }));
    vi.stubGlobal("fetch", fn);
    const { api } = await loadApi();

    await expect(api.logout()).resolves.toBeUndefined();
  });

  it("401 且本地有 token 时清除会话并广播 unauthorized", async () => {
    const { fn } = fetchMock(jsonResponse({ status: 401, body: { detail: "token 已失效" } }));
    vi.stubGlobal("fetch", fn);
    const { api, authEvents, getAuthToken, setAuthToken } = await loadApi();

    setAuthToken("stale");
    const listener = vi.fn();
    authEvents.addEventListener("unauthorized", listener);

    await expect(api.me()).rejects.toMatchObject({ status: 401, detail: "token 已失效" });
    expect(getAuthToken()).toBeNull();
    expect(localStorage.getItem("sm.auth.token")).toBeNull();
    expect(listener).toHaveBeenCalledTimes(1);
  });

  it("匿名请求的 401 不广播（避免登录页死循环）", async () => {
    const { fn } = fetchMock(jsonResponse({ status: 401, body: { detail: "需要登录" } }));
    vi.stubGlobal("fetch", fn);
    const { api, authEvents } = await loadApi();

    const listener = vi.fn();
    authEvents.addEventListener("unauthorized", listener);
    await expect(api.me()).rejects.toMatchObject({ status: 401 });
    expect(listener).not.toHaveBeenCalled();
  });

  it("字符串 detail 直接作为错误消息", async () => {
    const { fn } = fetchMock(jsonResponse({ status: 409, body: { detail: "名称已存在" } }));
    vi.stubGlobal("fetch", fn);
    const { api, ApiError } = await loadApi();

    const error = await api.createWorkspace({ name: "x" }).catch((err: unknown) => err);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 409, detail: "名称已存在", code: "", fieldErrors: {} });
  });

  it("FastAPI 422 列表 detail 聚合成字段错误", async () => {
    const body = {
      detail: [
        { loc: ["body", "username"], msg: "字段过短" },
        { loc: ["body", "password"], msg: "密码太弱" },
      ],
    };
    const { fn } = fetchMock(jsonResponse({ status: 422, body }));
    vi.stubGlobal("fetch", fn);
    const { api } = await loadApi();

    const error = await api.register({ username: "a", password: "b" }).catch((err: unknown) => err);
    expect(error).toMatchObject({
      status: 422,
      detail: "username: 字段过短; password: 密码太弱",
      fieldErrors: { username: "字段过短", password: "密码太弱" },
    });
  });

  it("X-Auth-Error 头优先于 body.code", async () => {
    const { fn } = fetchMock(
      jsonResponse({ status: 403, body: { code: "body_code", detail: "只读工作区" }, headers: { "X-Auth-Error": "read_only" } }),
    );
    vi.stubGlobal("fetch", fn);
    const { api } = await loadApi();

    await expect(api.createRule({})).rejects.toMatchObject({ code: "read_only", detail: "只读工作区" });
  });

  it("没有 X-Auth-Error 时用 body.code", async () => {
    const { fn } = fetchMock(jsonResponse({ status: 403, body: { code: "quota_exceeded", detail: "超限" } }));
    vi.stubGlobal("fetch", fn);
    const { api } = await loadApi();

    await expect(api.createRule({})).rejects.toMatchObject({ code: "quota_exceeded" });
  });

  it("非 JSON 错误体回退到通用文案", async () => {
    const { fn } = fetchMock(jsonResponse({ status: 502, rawBody: "<html>bad gateway</html>" }));
    vi.stubGlobal("fetch", fn);
    const { api } = await loadApi();

    await expect(api.health()).rejects.toMatchObject({ status: 502, detail: "请求失败 (HTTP 502)" });
  });
});

describe("查询参数拼装", () => {
  it("listStocks 省略空搜索并保留 limit/indices", async () => {
    const { fn, calls } = fetchMock(
      jsonResponse({ body: { total: 0, items: [] } }),
      jsonResponse({ body: { total: 0, items: [] } }),
    );
    vi.stubGlobal("fetch", fn);
    const { api } = await loadApi();

    await api.listStocks({ include_indices: true });
    expect(calls[0].url).toBe("/api/v1/stocks?limit=100&include_indices=true");

    await api.listStocks({ q: "茅台", limit: 5 });
    expect(calls[1].url).toBe(`/api/v1/stocks?q=${encodeURIComponent("茅台")}&limit=5`);
  });

  it("bars 默认前复权 500 根", async () => {
    const { fn, calls } = fetchMock(jsonResponse({ body: { code: "600519.SH", items: [] } }));
    vi.stubGlobal("fetch", fn);
    const { api } = await loadApi();

    await api.bars("600519.SH");
    expect(calls[0].url).toBe("/api/v1/stocks/600519.SH/bars?adjust=qfq&limit=500");
  });

  it("users 始终带分页参数", async () => {
    const { fn, calls } = fetchMock(jsonResponse({ body: { items: [], total: 0 } }));
    vi.stubGlobal("fetch", fn);
    const { api } = await loadApi();

    await api.users({ page: 3 });
    expect(calls[0].url).toBe("/api/v1/users?page=3&page_size=50");
  });

  it("runJob 把参数序列化到 query string", async () => {
    const { fn, calls } = fetchMock(jsonResponse({ body: { job: "sync", detail: {} } }));
    vi.stubGlobal("fetch", fn);
    const { api } = await loadApi();

    await api.runJob("sync_daily", { full: true, workers: 2 });
    expect(calls[0].url).toBe("/api/v1/system/jobs/sync_daily?full=true&workers=2");
    expect(calls[0].init.method).toBe("POST");
  });
});

/** jsdom 没有实现 createObjectURL，下载路径需要显式补上。 */
function stubBlobUrls(): void {
  Object.defineProperty(URL, "createObjectURL", { value: vi.fn(() => "blob:x"), configurable: true });
  Object.defineProperty(URL, "revokeObjectURL", { value: vi.fn(), configurable: true });
}

describe("文件下载", () => {
  it("按 Content-Disposition 决定文件名（含 UTF-8 编码）", async () => {
    const name = encodeURIComponent("工作区用量-20260101.csv");
    const { fn, calls } = fetchMock(
      jsonResponse({ rawBody: "a,b", headers: { "Content-Disposition": `attachment; filename*=UTF-8''${name}` } }),
    );
    vi.stubGlobal("fetch", fn);
    stubBlobUrls();

    const clicks: string[] = [];
    const originalClick = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function click(this: HTMLAnchorElement) {
      clicks.push(this.download);
    };

    try {
      const { api } = await loadApi();
      await api.downloadWorkspaceUsage(7);
    } finally {
      HTMLAnchorElement.prototype.click = originalClick;
    }

    expect(calls[0].url).toBe("/api/v1/workspaces/7/stats/export");
    expect(clicks).toEqual(["工作区用量-20260101.csv"]);
  });

  it("没有 Content-Disposition 时回退本地命名", async () => {
    const { fn } = fetchMock(jsonResponse({ rawBody: "x" }));
    vi.stubGlobal("fetch", fn);
    stubBlobUrls();

    const clicks: string[] = [];
    const originalClick = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function click(this: HTMLAnchorElement) {
      clicks.push(this.download);
    };
    try {
      const { api } = await loadApi();
      await api.downloadWorkspaceUsage(9);
    } finally {
      HTMLAnchorElement.prototype.click = originalClick;
    }

    expect(clicks).toEqual(["workspace-9-usage.csv"]);
  });
});
