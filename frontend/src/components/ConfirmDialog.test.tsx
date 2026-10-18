import type { ComponentProps } from "react";
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ConfirmDialog from "./ConfirmDialog";

function setup(overrides: Partial<ComponentProps<typeof ConfirmDialog>> = {}) {
  const onCancel = vi.fn();
  const onConfirm = vi.fn();
  const props = {
    open: true,
    title: "删除清单",
    description: "将同时删除其中的自选项",
    onCancel,
    onConfirm,
    ...overrides,
  };
  const utils = render(<ConfirmDialog {...props} />);
  return { ...utils, onCancel, onConfirm };
}

describe("ConfirmDialog", () => {
  it("open=false 时不渲染任何内容", () => {
    setup({ open: false });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("渲染标题、说明与自定义按钮文案", () => {
    setup({ confirmLabel: "删除", cancelLabel: "再想想" });
    expect(screen.getByRole("dialog")).toHaveAttribute("aria-modal", "true");
    expect(screen.getByRole("heading", { name: "删除清单" })).toBeInTheDocument();
    expect(screen.getByText("将同时删除其中的自选项")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "删除" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "再想想" })).toBeInTheDocument();
  });

  it("默认焦点落在取消上，避免误确认", async () => {
    setup();
    await new Promise((resolve) => requestAnimationFrame(resolve));
    expect(screen.getByRole("button", { name: "取消" })).toHaveFocus();
  });

  it("点击确认/取消触发对应回调", async () => {
    const user = userEvent.setup();
    const { onCancel, onConfirm } = setup();

    await user.click(screen.getByRole("button", { name: "确认" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);

    await user.click(screen.getByRole("button", { name: "取消" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("requireAcknowledge 时必须勾选才能确认", async () => {
    const user = userEvent.setup();
    const { onConfirm } = setup({ requireAcknowledge: true, acknowledgeLabel: "我已备份数据" });
    const confirm = screen.getByRole("button", { name: "确认" });

    expect(confirm).toBeDisabled();
    await user.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();

    await user.click(screen.getByRole("checkbox", { name: "我已备份数据" }));
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("busy 时按钮禁用并显示处理中，ESC 不再关闭", async () => {
    const user = userEvent.setup();
    const { onCancel } = setup({ busy: true });

    expect(screen.getByRole("button", { name: "处理中…" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "取消" })).toBeDisabled();

    await user.keyboard("{Escape}");
    expect(onCancel).not.toHaveBeenCalled();
  });

  it("非 busy 时 ESC 取消", async () => {
    const user = userEvent.setup();
    const { onCancel } = setup();

    await user.keyboard("{Escape}");
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("点击遮罩取消，点击面板内部不取消", async () => {
    const user = userEvent.setup();
    const { onCancel } = setup();
    const dialog = screen.getByRole("dialog");

    await user.click(dialog);
    expect(onCancel).not.toHaveBeenCalled();

    await user.click(dialog.parentElement as HTMLElement);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("重新打开时重置「已了解」勾选", async () => {
    const user = userEvent.setup();
    const { rerender, onCancel, onConfirm } = setup({ requireAcknowledge: true });

    await user.click(screen.getByRole("checkbox"));
    expect(screen.getByRole("button", { name: "确认" })).toBeEnabled();

    rerender(<ConfirmDialog open={false} title="删除清单" requireAcknowledge onCancel={onCancel} onConfirm={onConfirm} />);
    rerender(<ConfirmDialog open title="删除清单" requireAcknowledge onCancel={onCancel} onConfirm={onConfirm} />);

    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.getByRole("button", { name: "确认" })).toBeDisabled();
  });
});
