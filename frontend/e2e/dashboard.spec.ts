import { expect, test } from "@playwright/test";
import { expectLoggedIn, loginViaApi, openNav } from "./helpers";

// 用例标题统一用 ASCII：Playwright 会用标题生成失败产物目录名，
// 含非 ASCII 的路径在部分 Windows 安全策略下会写失败，反而掩盖真正的断言错误。
test.describe("dashboard and navigation", () => {
  test.beforeEach(async ({ page, request }) => {
    await loginViaApi(page, request);
    await page.goto("/");
    await expectLoggedIn(page);
  });

  test("renders all six index cards", async ({ page }) => {
    for (const name of ["上证指数", "深证成指", "创业板指", "沪深300", "中证500", "中证1000"]) {
      // 卡片里名称与代码在同一格，用包含匹配而非全等
      await expect(page.getByText(name).first()).toBeVisible();
    }
    await expect(page.getByText(/数据日期：\d{4}-\d{2}-\d{2}/)).toBeVisible();
  });

  test("shows market breadth and prediction top10", async ({ page }) => {
    await expect(page.getByText("市场宽度")).toBeVisible();
    await expect(page.getByText("预测上涨概率 Top 10")).toBeVisible();
    await expect(page.getByText("Mock A").first()).toBeVisible();
  });

  test("opens every primary page without runtime errors", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));

    const pages: Array<[string, RegExp]> = [
      ["行情", /\/market$/],
      ["自选", /\/watchlist$/],
      ["分析", /\/analyze$/],
      ["策略", /\/strategy$/],
      ["预测", /\/predict$/],
      ["回测", /\/backtest$/],
      ["预警", /\/alerts$/],
      ["设置", /\/settings$/],
      ["工作区", /\/workspace$/],
    ];

    for (const [label, url] of pages) {
      await openNav(page, label);
      await expect(page).toHaveURL(url);
      await expect(page.locator("main")).not.toBeEmpty();
    }

    expect(errors, `前端报错: ${errors.join(" | ")}`).toEqual([]);
  });

  test("unknown route falls back to dashboard", async ({ page }) => {
    await page.goto("/this-does-not-exist");
    await expect(page).toHaveURL(/\/$/);
  });
});
