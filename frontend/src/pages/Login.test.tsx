import { beforeEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Route, Routes } from "react-router-dom";
import { renderWithProviders } from "../test/utils";
import type { AuthConfig, AuthUser } from "../types";

vi.mock("../api", () => ({
  api: {
    authConfig: vi.fn(),
    me: vi.fn(),
    login: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
    wechatUrl: vi.fn(),
  },
  authEvents: new EventTarget(),
  getAuthToken: () => null,
  setAuthToken: vi.fn(),
}));

const { api } = await import("../api");
const { useAuth } = await import("../auth");
const { default: Login } = await import("./Login");

const LOGIN_API = api as unknown as {
  login: ReturnType<typeof vi.fn>;
  register: ReturnType<typeof vi.fn>;
  wechatUrl: ReturnType<typeof vi.fn>;
};

const CONFIG: AuthConfig = {
  auth_enabled: true,
  allow_register: true,
  wechat_enabled: false,
  wechat_mode: "open",
  has_users: true,
  workspace_enabled: true,
  version: "0.3.0",
};

const USER = { id: 1, username: "alice", role: "admin", status: "active" } as AuthUser;

function renderLogin(route = "/login?mode=login") {
  return renderWithProviders(
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/" element={<div>工作台首页</div>} />
      <Route path="/watchlist" element={<div>自选页</div>} />
    </Routes>,
    { route },
  );
}

beforeEach(() => {
  useAuth.setState({ ready: true, config: CONFIG, user: null });
  LOGIN_API.login.mockReset();
  LOGIN_API.register.mockReset();
});

describe("登录表单校验", () => {
  it("空提交只提示必填，不发请求", async () => {
    const user = userEvent.setup();
    renderLogin();

    await user.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByText("请输入用户名或邮箱")).toBeInTheDocument();
    expect(screen.getByText("请输入密码")).toBeInTheDocument();
    expect(LOGIN_API.login).not.toHaveBeenCalled();
  });

  it("输入后错误提示消失", async () => {
    const user = userEvent.setup();
    renderLogin();

    await user.click(screen.getByRole("button", { name: "登录" }));
    expect(await screen.findByText("请输入用户名或邮箱")).toBeInTheDocument();

    await user.type(screen.getByLabelText(/用户名 \/ 邮箱/), "alice");
    await waitFor(() => expect(screen.queryByText("请输入用户名或邮箱")).not.toBeInTheDocument());
  });

  it("登录成功跳转到 next 指定页面", async () => {
    const user = userEvent.setup();
    LOGIN_API.login.mockResolvedValue({ token: "tok", user: USER });
    renderLogin(`/login?mode=login&next=${encodeURIComponent("/watchlist")}`);

    await user.type(screen.getByLabelText(/用户名 \/ 邮箱/), "alice");
    await user.type(screen.getByLabelText("密码 *"), "pw123456");
    await user.click(screen.getByRole("button", { name: "登录" }));

    expect(await screen.findByText("自选页")).toBeInTheDocument();
    expect(LOGIN_API.login).toHaveBeenCalledWith("alice", "pw123456");
  });

  it("登录失败把后端消息显示在表单顶部", async () => {
    const user = userEvent.setup();
    LOGIN_API.login.mockRejectedValue(new Error("用户名或密码错误"));
    renderLogin();

    await user.type(screen.getByLabelText(/用户名 \/ 邮箱/), "alice");
    await user.type(screen.getByLabelText("密码 *"), "wrong");
    await user.click(screen.getByRole("button", { name: "登录" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("用户名或密码错误");
  });

  it("提交时去掉用户名两侧空白", async () => {
    const user = userEvent.setup();
    LOGIN_API.login.mockResolvedValue({ token: "tok", user: USER });
    renderLogin();

    await user.type(screen.getByLabelText(/用户名 \/ 邮箱/), "  alice  ");
    await user.type(screen.getByLabelText("密码 *"), "pw123456");
    await user.click(screen.getByRole("button", { name: "登录" }));

    await waitFor(() => expect(LOGIN_API.login).toHaveBeenCalledWith("alice", "pw123456"));
  });
});

describe("注册表单校验", () => {
  const submit = async (user: ReturnType<typeof userEvent.setup>) => {
    await user.click(screen.getByRole("button", { name: "注册并登录" }));
  };

  it("用户名不合法时拒绝提交", async () => {
    const user = userEvent.setup();
    renderLogin("/login?mode=register");

    await user.type(screen.getByLabelText("用户名 *"), "ab");
    await user.type(screen.getByLabelText("密码 *"), "pw123456");
    await user.type(screen.getByLabelText("确认密码 *"), "pw123456");
    await submit(user);

    expect(await screen.findByText(/用户名需 3-64 位/)).toBeInTheDocument();
    expect(LOGIN_API.register).not.toHaveBeenCalled();
  });

  it("密码缺少数字时提示规则", async () => {
    const user = userEvent.setup();
    renderLogin("/login?mode=register");

    await user.type(screen.getByLabelText("用户名 *"), "alice");
    await user.type(screen.getByLabelText("密码 *"), "onlyletters");
    await user.type(screen.getByLabelText("确认密码 *"), "onlyletters");
    await submit(user);

    expect(await screen.findByText(/密码至少 8 位，需同时包含字母和数字/)).toBeInTheDocument();
  });

  it("密码以空白开头时提示规则", async () => {
    const user = userEvent.setup();
    renderLogin("/login?mode=register");

    await user.type(screen.getByLabelText("用户名 *"), "alice");
    await user.type(screen.getByLabelText("密码 *"), " pw123456");
    await user.type(screen.getByLabelText("确认密码 *"), " pw123456");
    await submit(user);

    expect(await screen.findByText("密码不能以空白字符开头或结尾")).toBeInTheDocument();
  });

  it("两次密码不一致时提示", async () => {
    const user = userEvent.setup();
    renderLogin("/login?mode=register");

    await user.type(screen.getByLabelText("用户名 *"), "alice");
    await user.type(screen.getByLabelText("密码 *"), "pw123456");
    await user.type(screen.getByLabelText("确认密码 *"), "pw123457");
    await submit(user);

    expect(await screen.findByText("两次输入的新密码不一致")).toBeInTheDocument();
  });

  it("邮箱格式错误时定位到邮箱字段", async () => {
    const user = userEvent.setup();
    renderLogin("/login?mode=register");

    await user.type(screen.getByLabelText("用户名 *"), "alice");
    await user.type(screen.getByLabelText(/邮箱/), "alice@");
    await user.type(screen.getByLabelText("密码 *"), "pw123456");
    await user.type(screen.getByLabelText("确认密码 *"), "pw123456");
    await submit(user);

    expect(await screen.findByText("邮箱格式不正确")).toBeInTheDocument();
  });

  it("校验通过后调用注册接口并可省略可选字段", async () => {
    const user = userEvent.setup();
    LOGIN_API.register.mockResolvedValue({ token: "tok", user: USER });
    renderLogin("/login?mode=register");

    await user.type(screen.getByLabelText("用户名 *"), "alice");
    await user.type(screen.getByLabelText("密码 *"), "pw123456");
    await user.type(screen.getByLabelText("确认密码 *"), "pw123456");
    await submit(user);

    await waitFor(() =>
      expect(LOGIN_API.register).toHaveBeenCalledWith({
        username: "alice",
        password: "pw123456",
        email: undefined,
        nickname: undefined,
      }),
    );
    expect(await screen.findByText("工作台首页")).toBeInTheDocument();
  });

  it("用户名冲突时把错误挂到用户名输入框", async () => {
    const user = userEvent.setup();
    LOGIN_API.register.mockRejectedValue(new Error("用户名已被占用"));
    renderLogin("/login?mode=register");

    await user.type(screen.getByLabelText("用户名 *"), "alice");
    await user.type(screen.getByLabelText("密码 *"), "pw123456");
    await user.type(screen.getByLabelText("确认密码 *"), "pw123456");
    await submit(user);

    expect(await screen.findByText("用户名已被占用")).toBeInTheDocument();
    expect(screen.getByLabelText("用户名 *")).toHaveAttribute("aria-invalid", "true");
  });
});

describe("视图切换", () => {
  it("可在登录/注册之间切换，注册被关闭时不显示切换按钮", async () => {
    const user = userEvent.setup();
    const { unmount } = renderLogin();

    await user.click(screen.getByRole("tab", { name: "注册" }));
    expect(await screen.findByRole("button", { name: "注册并登录" })).toBeInTheDocument();
    unmount();

    useAuth.setState({ config: { ...CONFIG, allow_register: false } });
    renderLogin();
    expect(screen.queryByRole("tablist")).not.toBeInTheDocument();
    expect(screen.getByText(/已关闭自助注册/)).toBeInTheDocument();
  });

  it("关闭鉴权时提示可直接进入工作台", () => {
    useAuth.setState({ config: { ...CONFIG, auth_enabled: false } });
    renderLogin();

    expect(screen.getByText(/未开启登录校验/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /直接进入工作台/ })).toBeInTheDocument();
  });
});
