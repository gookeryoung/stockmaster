import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const ADMIN_USERNAME = process.env.E2E_USERNAME ?? "e2e-admin";
export const ADMIN_PASSWORD = process.env.E2E_PASSWORD ?? "e2e-Passw0rd";

const TOKEN_KEY = "sm.auth.token";

/** 通过 API 登录并把 token 注入 localStorage。
 *
 * 走 API 而不是 UI：每个用例都点一遍登录表单会把 e2e 时间浪费在与断言无关的表单上；
 * 登录 UI 本身在 `auth.spec.ts` 里单独覆盖。
 */
export async function loginViaApi(page: Page, request: APIRequestContext): Promise<string> {
  const response = await request.post("/api/v1/auth/login", {
    data: { username: ADMIN_USERNAME, password: ADMIN_PASSWORD },
  });
  expect(response.ok(), `登录失败: ${response.status()} ${await response.text()}`).toBeTruthy();
  const { token } = (await response.json()) as { token: string };

  await page.addInitScript(
    // 只在首次加载注入：init script 每次导航都会重跑，若每次都写回 token，
    // 「退出登录」后一刷新又被恢复成已登录，测试就失去了意义。
    ([key, value]) => {
      if (window.sessionStorage.getItem("e2e.token.injected")) return;
      window.localStorage.setItem(key, value);
      window.sessionStorage.setItem("e2e.token.injected", "1");
    },
    [TOKEN_KEY, token] as const,
  );
  return token;
}

/** 等待页面进入「已登录且工作区就绪」形态。
 *
 * 账号菜单出现只说明会话已恢复；`Layout` 还会用 `key={currentId}` 给路由子树换 key，
 * 工作区从 null 变成实际 id 时整棵子树会重新挂载 —— 此时输入框内容与 mutation 结果
 * 都会被清掉。因此必须等工作区选择器就绪后，再开始交互。
 */
export async function expectLoggedIn(page: Page): Promise<void> {
  await expect(page.getByRole("button", { name: "账号菜单" })).toBeVisible({ timeout: 20_000 });
  const workspaceSelect = page.locator("select").first();
  if (await workspaceSelect.count()) {
    await expect(workspaceSelect).toBeEnabled({ timeout: 20_000 });
  }
  // 换 key 的重挂载与选择器可用几乎同时发生，留一拍确保页面稳定。
  await page.waitForTimeout(300);
}

export async function openNav(page: Page, label: string): Promise<void> {
  await page.getByRole("link", { name: label, exact: true }).click();
}
