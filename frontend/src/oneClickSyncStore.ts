/** 「一键拉取日线数据」的运行状态，放在模块级 store。

 * 路由切换会卸载设置页组件，组件内的 useState 随之丢失——正在跑的链路状态也会
 * 消失：再次点击会另起一条链路，与仍在后台执行的前一条撞车（后端任务同步串行）
 * 而卡死。这里把运行状态与中止句柄放到模块级 store（不持久化，刷新即丢），切页
 * 重挂载后仍能读到「进行中」与停止入口；running 期间也禁止重复启动。
 *
 * 顺序执行与失败定位交给 ``runOneClickSync``，这里只管状态与生命周期。
 */

import { create } from "zustand";
import { api } from "./api";
import { initialStepStates, runOneClickSync, type StepState } from "./oneClickSync";

interface OneClickSyncState {
  steps: StepState[] | null;
  /** 是否有一条链路正在执行；true 时禁止重复启动。 */
  running: boolean;
  /** 最近一次结束（完成 / 失败 / 停止）后的总结文案。 */
  result: string | null;
  start: () => Promise<void>;
  stop: () => void;
}

/** 中止句柄不进 store：它是瞬时对象，且需要保持状态可序列化。 */
let controller: AbortController | null = null;

export const useOneClickSync = create<OneClickSyncState>((set, get) => ({
  steps: null,
  running: false,
  result: null,

  start: async () => {
    // 已在运行就直接返回，避免第二条链路并发打后端任务接口。
    if (get().running) return;
    const active = new AbortController();
    controller = active;
    set({ running: true, result: null, steps: initialStepStates() });
    try {
      const final = await runOneClickSync(
        (job, params, signal) => api.runJob(job, params ?? {}, signal),
        (steps) => set({ steps }),
        { signal: active.signal },
      );
      const stopped = final.some((step) => step.status === "stopped");
      const failed = final.filter((step) => step.status === "failed");
      set({
        result: stopped
          ? "一键拉取已停止。"
          : failed.length
            ? `一键拉取结束，${failed.length} 个环节失败：${failed.map((step) => step.label).join("、")}`
            : "一键拉取完成。",
      });
    } finally {
      controller = null;
      set({ running: false });
    }
  },

  // 只有显式点击才中止：链路本身不会自行停止。
  stop: () => controller?.abort(),
}));
