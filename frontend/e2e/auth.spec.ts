import { expect, test } from "@playwright/test";
import { ADMIN_PASSWORD, ADMIN_USERNAME, expectLoggedIn, loginViaApi } from "./helpers";

// 标题用 ASCII，理由见 dashboard.spec.ts。
test.describe("authentication", () => {
  test("redirects anonymous visitors to login with next", async ({ page }) => {
    await page.goto("/watchlist");

    await expect(page).toHaveURL(/\/login\?next=%2Fwatchlist/);
    await expect(page.getByRole("form", { name: "登录" })).toBeVisible();
  });

  test("blocks empty submit client side", async ({ page }) => {
    await page.goto("/login?mode=login");
    await page.getByRole("button", { name: "登录", exact: true }).click();

    await expect(page.getByText("请输入用户名或邮箱")).toBeVisible();
    await expect(page.getByText("请输入密码")).toBeVisible();
  });

  test("shows backend message on wrong password", async ({ page }) => {
    await page.goto("/login?mode=login");
    await page.getByLabel(/用户名 \/ 邮箱/).fill(ADMIN_USERNAME);
    await page.getByLabel("密码 *").fill("definitely-wrong-1");
    await page.getByRole("button", { name: "登录", exact: true }).click();

    await expect(page.getByRole("alert")).toContainText(/用户名|密码|错误/);
    await expect(page).toHaveURL(/\/login/);
  });

  test("logs in and lands on the dashboard", async ({ page }) => {
    await page.goto("/login?mode=login");
    await page.getByLabel(/用户名 \/ 邮箱/).fill(ADMIN_USERNAME);
    await page.getByLabel("密码 *").fill(ADMIN_PASSWORD);
    await page.getByRole("button", { name: "登录", exact: true }).click();

    await expectLoggedIn(page);
    await expect(page).toHaveURL(/\/$/);
    await expect(page.getByRole("link", { name: "总览", exact: true })).toBeVisible();
  });

  test("keeps the session across reloads", async ({ page, request }) => {
    await loginViaApi(page, request);

    await page.goto("/");
    await expectLoggedIn(page);

    await page.reload();
    await expectLoggedIn(page);
  });

  test("logout clears the local session", async ({ page, request }) => {
    await loginViaApi(page, request);
    await page.goto("/");
    await expectLoggedIn(page);

    await page.getByRole("button", { name: "账号菜单" }).click();
    await page.getByRole("menuitem", { name: "退出登录" }).click();

    await expect(page).toHaveURL(/\/login/);
    // URL 先变、文档后换：此时 page.evaluate 会撞上
    // "Execution context was destroyed, most likely because of a navigation"。
    // 先等登录页真正渲染，再读 localStorage。
    await expect(page.getByRole("form", { name: "登录" })).toBeVisible();
    const token = await page.evaluate(() => window.localStorage.getItem("sm.auth.token"));
    expect(token).toBeNull();
  });

  test("revoked token bounces back to login", async ({ page, request }) => {
    const token = await loginViaApi(page, request);
    await page.goto("/");
    await expectLoggedIn(page);

    // 服务端吊销这个 token（等同「在别的设备退出登录」），本地仍持有旧 token
    await request.post("/api/v1/auth/logout", { headers: { Authorization: `Bearer ${token}` } });
    await page.reload();

    await expect(page).toHaveURL(/\/login/, { timeout: 20_000 });
  });
});
