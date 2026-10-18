import { expect, test } from "@playwright/test";
import { expectLoggedIn, loginViaApi } from "./helpers";

// 标题用 ASCII，理由见 dashboard.spec.ts。
test.describe("backtest", () => {
  test.beforeEach(async ({ page, request }) => {
    await loginViaApi(page, request);
    await page.goto("/backtest");
    await expectLoggedIn(page);
  });

  test("buy and hold produces the full metric set", async ({ page }) => {
    await page.getByRole("button", { name: "开始回测" }).click();

    await expect(page.getByText("总收益")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText("最大回撤")).toBeVisible();
    await expect(page.getByText("夏普")).toBeVisible();
    await expect(page.getByText("成交笔数")).toBeVisible();
    // 净值曲线渲染为 canvas
    await expect(page.locator("canvas").first()).toBeVisible();
  });

  test("empty codes falls back to the default universe", async ({ page }) => {
    await page.locator("label:has-text('标的（逗号分隔）') + input").fill("");
    await page.getByRole("button", { name: "开始回测" }).click();

    // 空清单由后端回落到默认池，页面不应崩在空白态
    await expect(page.getByText("总收益")).toBeVisible({ timeout: 30_000 });
  });

  test("unknown symbol surfaces a readable error", async ({ page }) => {
    await page.locator("label:has-text('标的（逗号分隔）') + input").fill("NOT-A-CODE");
    await page.getByRole("button", { name: "开始回测" }).click();

    await expect(page.getByText(/no stored bars|没有|无|失败/i).first()).toBeVisible({ timeout: 30_000 });
  });
});

test.describe("prediction", () => {
  test("running prediction fills probabilities", async ({ page, request }) => {
    await loginViaApi(page, request);
    await page.goto("/predict");
    await expectLoggedIn(page);

    await page.getByRole("button", { name: "运行预测" }).click();

    await expect(page.getByText(/Mock [AB]/).first()).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText(/\d+\.\d%/).first()).toBeVisible();
  });
});

test.describe("alerts", () => {
  test("creates a rule and scans it", async ({ page, request }) => {
    await loginViaApi(page, request);
    await page.goto("/alerts");
    await expectLoggedIn(page);

    const name = `e2e rule ${Date.now()}`;
    await page.locator("label:has-text('规则名称') + input").fill(name);
    await page.getByRole("button", { name: "创建规则" }).click();

    await expect(page.getByText(name).first()).toBeVisible({ timeout: 20_000 });

    await page.getByRole("button", { name: "立即扫描" }).click();
    // 扫描不应把页面打回空白或错误态
    await expect(page.getByText(name).first()).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText(/失败|错误/)).toHaveCount(0);
  });
});
