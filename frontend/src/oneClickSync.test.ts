/** 「一键拉取」编排的行为测试：顺序、状态流转、失败定位。 */

import { describe, expect, it, vi } from "vitest";
import { SYNC_STEPS, initialStepStates, runOneClickSync, stepMessage, type StepState } from "./oneClickSync";

describe("initialStepStates", () => {
  it("覆盖链路全部环节且初始为等待中", () => {
    const states = initialStepStates();
    expect(states.map((s) => s.key)).toEqual(["calendar", "instruments", "index_daily", "daily"]);
    expect(states.every((s) => s.status === "pending")).toBe(true);
  });
});

describe("stepMessage", () => {
  it("优先使用后端中文 message", () => {
    expect(stepMessage({ message: "同步完成：处理 2 项，写入 10 行。" })).toBe("同步完成：处理 2 项，写入 10 行。");
  });

  it("缺失 message 时用计数兜底", () => {
    expect(stepMessage({ processed: 3, bars_written: 12, failed: 1 })).toBe("处理 3，写入 12，失败 1");
  });
});

describe("runOneClickSync", () => {
  it("按依赖顺序调用每个环节，并逐步推送状态", async () => {
    const runJob = vi.fn(async (job: string) => ({ job, detail: { message: `${job} 完成`, processed: 1, failed: 0 } }));
    const snapshots: StepState[][] = [];

    const final = await runOneClickSync(runJob, (steps) => snapshots.push(steps), { params: {} });

    expect(runJob.mock.calls.map(([job]) => job)).toEqual(["calendar", "instruments", "index_daily", "daily"]);
    expect(final.every((s) => s.status === "done")).toBe(true);
    expect(final.map((s) => s.message)).toEqual([
      "calendar 完成",
      "instruments 完成",
      "index_daily 完成",
      "daily 完成",
    ]);
    // 每一步至少推送「进行中」与「完成」两次。
    expect(snapshots.length).toBeGreaterThanOrEqual(SYNC_STEPS.length * 2);
    expect(snapshots.some((steps) => steps[0].status === "running")).toBe(true);
  });

  it("后端报 failed 的环节标记为失败但不中断后续环节", async () => {
    const runJob = vi.fn(async (job: string) => ({
      job,
      detail: job === "index_daily"
        ? { message: "部分完成：写入 5 行，1 个指数失败。", failed: 1 }
        : { message: `${job} 完成`, failed: 0 },
    }));

    const final = await runOneClickSync(runJob, () => {}, {});

    expect(runJob).toHaveBeenCalledTimes(4);
    const indexStep = final.find((s) => s.key === "index_daily")!;
    expect(indexStep.status).toBe("failed");
    expect(indexStep.message).toContain("失败");
    // 失败环节之后的 daily 仍然执行了。
    expect(final.find((s) => s.key === "daily")!.status).toBe("done");
  });

  it("请求抛错时把该环节标为失败并保留错误信息，其余照跑", async () => {
    const runJob = vi.fn(async (job: string) => {
      if (job === "instruments") throw new Error("HTTP 500");
      return { job, detail: { message: `${job} 完成`, failed: 0 } };
    });

    const final = await runOneClickSync(runJob, () => {}, {});

    const instruments = final.find((s) => s.key === "instruments")!;
    expect(instruments.status).toBe("failed");
    expect(instruments.message).toContain("HTTP 500");
    expect(final.find((s) => s.key === "calendar")!.status).toBe("done");
    expect(final.find((s) => s.key === "daily")!.status).toBe("done");
  });

  it("signal 已中止时不启动任何环节，整条链路标为已停止", async () => {
    const runJob = vi.fn(async (job: string) => ({ job, detail: { message: `${job} 完成`, failed: 0 } }));
    const controller = new AbortController();
    controller.abort();

    const final = await runOneClickSync(runJob, () => {}, { signal: controller.signal });

    expect(runJob).not.toHaveBeenCalled();
    expect(final.every((s) => s.status === "stopped")).toBe(true);
  });

  it("执行中被中止：当前与后续环节标为已停止，已完成环节保留", async () => {
    const controller = new AbortController();
    const runJob = vi.fn(async (job: string) => {
      if (job === "instruments") {
        // 模拟点击「停止」：中止在途请求后抛出 AbortError。
        controller.abort();
        throw new Error("已中止");
      }
      return { job, detail: { message: `${job} 完成`, failed: 0 } };
    });

    const final = await runOneClickSync(runJob, () => {}, { signal: controller.signal });

    expect(runJob).toHaveBeenCalledTimes(2);
    expect(final.find((s) => s.key === "calendar")!.status).toBe("done");
    expect(final.find((s) => s.key === "instruments")!.status).toBe("stopped");
    expect(final.find((s) => s.key === "index_daily")!.status).toBe("stopped");
    expect(final.find((s) => s.key === "daily")!.status).toBe("stopped");
  });
});
