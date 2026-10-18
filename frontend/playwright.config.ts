import { defineConfig, devices } from "@playwright/test";

/** E2E 配置。
 *
 * 与单测的分工：单测覆盖纯逻辑与组件分支，这里覆盖「真实浏览器 + 真实后端 + 真实数据库」
 * 的关键用户流程。后端由 `scripts/e2e_server.py` 起，数据来自 MockProvider 但走完整的
 * 同步/指标/训练链路，所以失败信息通常指向真正的集成问题。
 */

const PORT = Number(process.env.E2E_PORT ?? 8799);
const BASE_URL = `http://127.0.0.1:${PORT}`;
/** CI 里用 `uv run python`，本地可以直接给解释器路径。 */
const PYTHON = process.env.E2E_PYTHON ?? "python";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 10_000 },
  // 共享一个数据库实例：并行会让自选/回测等写操作互相干扰。
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI
    ? [["list"], ["github"], ["html", { open: "never" }], ["junit", { outputFile: "e2e-report/junit.xml" }]]
    : [["list"], ["html", { open: "never" }]],
  outputDir: process.env.E2E_OUTPUT_DIR ?? "test-results",
  use: {
    baseURL: BASE_URL,
    // 本地默认不落盘：产物目录名取自用例标题（含中文），在部分 Windows 环境下会被
    // 安全策略拦截；CI 上再打开，失败时仍有 trace/video 可查。
    trace: process.env.CI ? "retain-on-failure" : "off",
    screenshot: process.env.CI ? "only-on-failure" : "off",
    video: process.env.CI ? "retain-on-failure" : "off",
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `${PYTHON} scripts/e2e_server.py --port ${PORT}`,
    url: `${BASE_URL}/api/v1/health`,
    cwd: "..",
    reuseExistingServer: !process.env.CI,
    timeout: 240_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
