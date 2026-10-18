import { expect, test } from "@playwright/test";
import { expectLoggedIn, loginViaApi } from "./helpers";

// 标题用 ASCII，理由见 dashboard.spec.ts。
test.describe("market page", () => {
  test.beforeEach(async ({ page, request }) => {
    await loginViaApi(page, request);
    await page.goto("/market");
    await expectLoggedIn(page);
  });

  test("switching symbol renders the kline chart", async ({ page }) => {
    await expect(page.getByRole("cell", { name: "600519.SH" })).toBeVisible();

    await page.getByRole("cell", { name: "000001.SZ" }).click();

    await expect(page.getByText(/Mock B/).first()).toBeVisible();
    await expect(page.getByText(/\d+ 根日线/)).toBeVisible();
    // ECharts 渲染为 canvas；有 canvas 说明 K 线组件真的挂载了
    await expect(page.locator("canvas").first()).toBeVisible();
  });

  test("search filters the symbol list", async ({ page }) => {
    await page.getByPlaceholder(/搜索代码或名称/).fill("600519");
    await expect(page.getByRole("cell", { name: "600519.SH" })).toBeVisible();
    await expect(page.getByRole("cell", { name: "000001.SZ" })).toHaveCount(0);
  });

  test("switching adjust mode keeps data", async ({ page }) => {
    await page.getByLabel("复权方式").selectOption("raw");
    await expect(page.getByText(/Mock A/).first()).toBeVisible();
    await expect(page.getByText(/\d+ 根日线/)).toBeVisible();
  });

  test("add-to-watchlist flips the button state", async ({ page }) => {
    await page.getByRole("cell", { name: "000001.SZ" }).click();
    const addButton = page.getByRole("button", { name: /加入自选|已在自选/ });
    // 自选状态可能被其它用例改过，这里只断言「最终收敛到已在自选」
    if ((await addButton.textContent())?.includes("加入自选")) {
      await addButton.click();
    }
    await expect(page.getByRole("button", { name: "已在自选" })).toBeVisible();
  });
});

test.describe("watchlist page", () => {
  test("watchlist add then remove round trip", async ({ page, request }) => {
    const token = await loginViaApi(page, request);
    const auth = { Authorization: `Bearer ${token}` };

    // 用接口清场（arrange），把 UI 留给真正的断言对象：添加与移出。
    const listed = await request.get("/api/v1/watchlist", { headers: auth });
    expect(listed.ok()).toBeTruthy();
    for (const item of (await listed.json()) as Array<{ id: number; code: string }>) {
      if (item.code === "600519.SH") {
        await request.delete(`/api/v1/watchlist/${item.id}`, { headers: auth });
      }
    }

    await page.goto("/watchlist");
    await expectLoggedIn(page);

    await page.getByLabel("添加自选：搜索代码或名称").fill("600519");
    await page.getByRole("button", { name: /600519\.SH/ }).first().click();
    await expect(page.getByRole("row", { name: /600519/ })).toBeVisible();

    await page.getByRole("row", { name: /600519/ }).getByRole("button", { name: "移出" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText("移出「600519.SH」？");
    await dialog.getByRole("button", { name: "移出" }).click();

    await expect(page.getByRole("row", { name: /600519/ })).toHaveCount(0);
  });
});
