/** 测试公共工具：渲染包装与 fetch 替身。 */

import type { ReactElement, ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, type RenderOptions } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { vi } from "vitest";

/** QueryClient 关掉重试：失败用例要立刻看到错误态而不是等到超时。 */
export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });
}

interface Options extends Omit<RenderOptions, "wrapper"> {
  route?: string;
}

/** 渲染带 Router + React Query 的组件树。 */
export function renderWithProviders(ui: ReactElement, { route = "/", ...options }: Options = {}) {
  const client = makeQueryClient();
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <QueryClientProvider client={client}>
        <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
      </QueryClientProvider>
    );
  }
  return { client, ...render(ui, { wrapper: Wrapper, ...options }) };
}

export interface FakeResponseInit {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
  /** 直接返回原始文本（模拟代理返回 HTML 错误页）。 */
  rawBody?: string;
}

/** 构造一个最小可用的 `Response` 替身。 */
export function jsonResponse({ status = 200, body, headers = {}, rawBody }: FakeResponseInit = {}): Response {
  const payload = rawBody ?? (body === undefined ? "" : JSON.stringify(body));
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(headers),
    json: async () => {
      if (rawBody !== undefined) throw new SyntaxError("Unexpected token < in JSON");
      return body;
    },
    text: async () => payload,
    blob: async () => new Blob([payload]),
  } as unknown as Response;
}

/** 记录调用并按顺序返回预设响应的 fetch 替身。 */
export function fetchMock(...responses: Array<Response | (() => Response)>) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  const queue = [...responses];
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    calls.push({ url: String(input), init });
    const next = queue.shift();
    if (!next) throw new Error(`unexpected fetch: ${String(input)}`);
    return typeof next === "function" ? next() : next;
  });
  return { fn, calls };
}

export { vi } from "vitest";
