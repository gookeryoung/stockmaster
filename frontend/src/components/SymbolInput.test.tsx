import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import SymbolInput from "./SymbolInput";
import { jsonResponse, renderWithProviders } from "../test/utils";
import type { Instrument } from "../types";

const CATALOG: Instrument[] = [
  { code: "600519.SH", name: "贵州茅台", market: "SH", board: "main", is_index: false, is_st: false, status: 1, list_date: null },
  { code: "000001.SZ", name: "平安银行", market: "SZ", board: "main", is_index: false, is_st: false, status: 1, list_date: null },
  { code: "000300.SH", name: "沪深300", market: "SH", board: "main", is_index: true, is_st: false, status: 1, list_date: null },
];

/** 用 `/stocks` 的响应形状（代码或名称包含关键词即命中）替代真实后端。 */
function mockStocks() {
  const fn = vi.fn(async (input: RequestInfo | URL) => {
    const url = new URL(String(input), "http://localhost");
    const query = url.searchParams.get("q") ?? "";
    const items = CATALOG.filter(
      (item) => item.code.includes(query) || item.name.includes(query),
    );
    return jsonResponse({ body: { total: items.length, items } });
  });
  vi.stubGlobal("fetch", fn);
  return fn;
}

/** 受控包装：让输入框真的随 onchange 更新，才能模拟连续键入与点选。 */
function Harness({
  multiple = false,
  initial = "",
  includeIndices = false,
  onChange,
}: {
  multiple?: boolean;
  initial?: string;
  includeIndices?: boolean;
  onChange?: (value: string) => void;
}) {
  const [value, setValue] = useState(initial);
  return (
    <SymbolInput
      value={value}
      multiple={multiple}
      includeIndices={includeIndices}
      aria-label="标的"
      onChange={(next) => {
        setValue(next);
        onChange?.(next);
      }}
    />
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("SymbolInput", () => {
  it("按代码联想并点选写回完整代码", async () => {
    mockStocks();
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderWithProviders(<Harness onChange={onChange} />);

    await user.type(screen.getByLabelText("标的"), "600519");
    const option = await screen.findByRole("option", { name: /600519\.SH/ });
    expect(option).toHaveTextContent("贵州茅台");

    await user.click(option);
    expect(onChange).toHaveBeenLastCalledWith("600519.SH");
    expect(screen.getByLabelText("标的")).toHaveValue("600519.SH");
  });

  it("按名称联想：输入中文名称也能命中", async () => {
    mockStocks();
    const user = userEvent.setup();
    renderWithProviders(<Harness />);

    await user.type(screen.getByLabelText("标的"), "平安");
    expect(await screen.findByRole("option", { name: /000001\.SZ/ })).toHaveTextContent("平安银行");
  });

  it("多标的模式只替换最后一段并保留已录入的代码", async () => {
    mockStocks();
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderWithProviders(<Harness multiple initial="600519.SH, " onChange={onChange} />);

    await user.type(screen.getByLabelText("标的"), "0000");
    await user.click(await screen.findByRole("option", { name: /000001\.SZ/ }));

    expect(onChange).toHaveBeenLastCalledWith("600519.SH, 000001.SZ");
  });

  it("多标的模式下已选代码标为「已选」且不可再点选", async () => {
    mockStocks();
    const user = userEvent.setup();
    renderWithProviders(<Harness multiple initial="600519.SH, " />);

    await user.type(screen.getByLabelText("标的"), "600519");
    const option = await screen.findByRole("option", { name: /600519\.SH/ });

    expect(option).toBeDisabled();
    expect(option).toHaveTextContent("已选");
  });

  it("无匹配时给出提示", async () => {
    mockStocks();
    const user = userEvent.setup();
    renderWithProviders(<Harness />);

    await user.type(screen.getByLabelText("标的"), "ZZZZ");
    expect(await screen.findByText(/没有匹配的标的/)).toBeInTheDocument();
  });

  it("方向键 + 回车可选中高亮项", async () => {
    mockStocks();
    const user = userEvent.setup();
    const onChange = vi.fn();
    renderWithProviders(<Harness multiple onChange={onChange} />);

    const input = screen.getByLabelText("标的");
    await user.type(input, "000300");
    await screen.findByRole("option", { name: /000300\.SH/ });

    await user.keyboard("{Enter}");
    expect(onChange).toHaveBeenLastCalledWith("000300.SH");
  });

  it("includeIndices 透传为查询参数", async () => {
    const fn = mockStocks();
    const user = userEvent.setup();
    renderWithProviders(<Harness includeIndices />);

    await user.type(screen.getByLabelText("标的"), "000300");
    await screen.findByRole("option", { name: /000300\.SH/ });

    await waitFor(() => {
      const urls = fn.mock.calls.map(([input]) => String(input));
      expect(urls.some((url) => url.includes("include_indices=true"))).toBe(true);
    });
  });
});
