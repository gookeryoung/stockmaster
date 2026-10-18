import { describe, expect, it, vi, afterEach } from "vitest";
import {
  changeColor,
  formatCompact,
  formatDateTime,
  formatNumber,
  formatPct,
  sideColor,
  todayISO,
  yearsAgoISO,
} from "./format";

describe("formatPct", () => {
  it("正数带 +、负数保留符号", () => {
    expect(formatPct(1.234)).toBe("+1.23%");
    expect(formatPct(-1.235)).toBe("-1.24%");
  });

  it("零不带符号", () => {
    expect(formatPct(0)).toBe("0.00%");
  });

  it("空值/NaN 回退占位符", () => {
    expect(formatPct(null)).toBe("--");
    expect(formatPct(undefined)).toBe("--");
    expect(formatPct(Number.NaN)).toBe("--");
  });

  it("支持自定义小数位", () => {
    expect(formatPct(3.14159, 4)).toBe("+3.1416%");
    expect(formatPct(3.14159, 0)).toBe("+3%");
  });
});

describe("formatNumber / formatCompact", () => {
  it("formatNumber 空值回退占位符，否则定点输出", () => {
    expect(formatNumber(undefined)).toBe("--");
    expect(formatNumber(1234.5678)).toBe("1234.57");
    expect(formatNumber(0, 0)).toBe("0");
  });

  it("formatCompact 按亿/万分级", () => {
    expect(formatCompact(123_456_789)).toBe("1.23亿");
    expect(formatCompact(-123_456_789)).toBe("-1.23亿");
    expect(formatCompact(12_345)).toBe("1.23万");
    expect(formatCompact(9_999)).toBe("9999.00");
    expect(formatCompact(null)).toBe("--");
  });
});

describe("changeColor（A 股约定：红涨绿跌）", () => {
  it("涨用 text-up、跌用 text-down、平用 text-flat", () => {
    expect(changeColor(1)).toBe("text-up");
    expect(changeColor(-1)).toBe("text-down");
    expect(changeColor(0)).toBe("text-flat");
  });

  it("缺失值视为平盘", () => {
    expect(changeColor(null)).toBe("text-flat");
    expect(changeColor(Number.NaN)).toBe("text-flat");
  });

  it("sideColor 买入为涨色、卖出为跌色", () => {
    expect(sideColor("buy")).toBe("text-up");
    expect(sideColor("sell")).toBe("text-down");
  });
});

describe("日期工具", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("todayISO 输出 YYYY-MM-DD", () => {
    expect(todayISO()).toMatch(/^\d{4}-\d{2}-\d{2}$/);
  });

  it("yearsAgoISO 回退指定年数", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-18T08:00:00Z"));
    expect(yearsAgoISO(2)).toBe("2024-10-18");
    expect(yearsAgoISO(0)).toBe("2026-10-18");
  });

  it("formatDateTime 把无时区的后端时间按 UTC 解析", () => {
    expect(formatDateTime("2026-01-02T03:04:05")).toMatch(/^2026-01-02 \d{2}:\d{2}$/);
  });

  it("formatDateTime 保留显式时区信息", () => {
    expect(formatDateTime("2026-01-02T03:04:05Z")).toBe(
      new Date("2026-01-02T03:04:05Z").toLocaleString("sv-SE").slice(0, 16),
    );
  });

  it("formatDateTime 对空值与非法值不崩溃", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime("")).toBe("—");
    expect(formatDateTime("not-a-date")).toBe("not-a-date");
  });
});
