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

// Node 25+ 默认暴露一个无方法的原生 globalThis.localStorage 桩（未配置
// --localstorage-file 时只有空对象），会遮蔽 jsdom 的实现，导致
// localStorage.clear() 抛 TypeError。检测到破损实现时换成纯内存 Storage。
class MemoryStorage {
  private map = new Map<string, string>();
  get length(): number {
    return this.map.size;
  }
  clear(): void {
    this.map.clear();
  }
  getItem(key: string): string | null {
    return this.map.get(key) ?? null;
  }
  key(index: number): string | null {
    return [...this.map.keys()][index] ?? null;
  }
  removeItem(key: string): void {
    this.map.delete(key);
  }
  setItem(key: string, value: string): void {
    this.map.set(key, String(value));
  }
}

// 用 defineProperty 而不是 vi.stubGlobal：测试文件里的 vi.unstubAllGlobals()
// 会把 stub 还原成 Node 原生坏桩，而直接覆盖属性不受其影响。
if (typeof globalThis.localStorage?.clear !== "function") {
  for (const key of ["localStorage", "sessionStorage"] as const) {
    Object.defineProperty(globalThis, key, {
      value: new MemoryStorage(),
      configurable: true,
      enumerable: true,
      writable: true,
    });
  }
}

beforeEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

afterEach(async () => {
  cleanup();
  // 冲刷上一个用例遗留的 requestAnimationFrame 回调（如 Login 的 focusField）。
  // 不冲刷的话，它会在下一个用例 user.type 输入中途触发并抢走焦点，
  // 导致字符落错字段、校验失败。cleanup 后 DOM 已卸载，遗留回调安全落空。
  await new Promise((resolve) => requestAnimationFrame(resolve));
});
