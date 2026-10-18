/** Vitest 全局初始化。
 *
 * 只放「jsdom 缺什么补什么」与全局清理：任何与业务相关的准备都应留在测试文件里，
 * 否则单个测试的失败原因会变得难以定位。
 */

import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach, vi } from "vitest";

// jsdom 没有实现 ResizeObserver：EChart / KlineChart 在挂载时就会用到。
class ResizeObserverStub {
  observe(): void {}
  unobserve(): void {}
  disconnect(): void {}
}

if (!("ResizeObserver" in globalThis)) {
  vi.stubGlobal("ResizeObserver", ResizeObserverStub);
}

// jsdom 的 matchMedia 未实现，Tailwind 断点相关代码会直接抛错。
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

// 个别组件用 scrollIntoView 滚动到错误字段
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(() => {
  cleanup();
});
