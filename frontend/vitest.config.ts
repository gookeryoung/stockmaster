/** Vitest 配置（单元测试）。
 *
 * 与 `vite.config.ts` 分开：构建配置需要 manualChunks 等产物相关选项，测试不需要；
 * 拆开后 `vitest` 不会误加载构建插件链，启动更快，也避免 build 与 test 的选项互相污染。
 *
 * 覆盖率阈值按「逻辑模块」而不是全仓设定：页面的渲染分支由 Playwright e2e 覆盖，
 * 用全局阈值逼着给每个页面写渲染断言只会产出只跑不验的测试。
 */

import { fileURLToPath, URL } from "node:url";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    // e2e 由 Playwright 跑，不要被 vitest 收集
    exclude: ["e2e/**", "node_modules/**", "dist/**"],
    restoreMocks: true,
    clearMocks: true,
    coverage: {
      provider: "v8",
      reporter: ["text", "json-summary", "lcov"],
      reportsDirectory: "coverage",
      include: ["src/**/*.{ts,tsx}"],
      exclude: [
        "src/**/*.test.{ts,tsx}",
        "src/test/**",
        "src/types.ts",
        "src/vite-env.d.ts",
        "src/main.tsx",
      ],
      thresholds: {
        // 只给「纯逻辑」模块设门槛：api.ts 里大量薄封装的价值在于签名而非分支，
        // 页面渲染分支交给 Playwright e2e，用全局阈值只会逼出空转的断言。
        "src/format.ts": { lines: 95, branches: 90, functions: 100, statements: 95 },
        "src/auth.ts": { lines: 85, branches: 80, functions: 75, statements: 85 },
        "src/view.ts": { lines: 95, branches: 80, functions: 70, statements: 95 },
        "src/workspace.ts": { lines: 75, branches: 70, functions: 70, statements: 75 },
        "src/components/ConfirmDialog.tsx": { lines: 95, branches: 90, functions: 100, statements: 95 },
      },
    },
  },
});
