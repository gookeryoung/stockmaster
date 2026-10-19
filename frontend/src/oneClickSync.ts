/** 「一键拉取日线数据」的执行编排。
 *
 * 后端每个环节都是独立、同步的 job 接口（``POST /system/jobs/{job}``），所以
 * 「实时进度」由前端按顺序逐个调用、每步开始/结束都刷新状态得到——接口返回即
 * 可判定该环节成功还是失败，不需要后端推流。这样既复用现有接口，又能在失败时
 * 精确定位到具体环节。
 *
 * 依赖顺序是固定的：先有交易日历才知道哪天开市，先有证券主数据才知道拉哪些标的；
 * 指数日线独立于个股日线，但都排在同一个链路里。
 */

export interface SyncStep {
  key: string;
  label: string;
  hint: string;
}

/** 日线数据链路：交易日历 → 证券主数据 → 指数日线 → 个股日线。 */
export const SYNC_STEPS: SyncStep[] = [
  { key: "calendar", label: "同步交易日历", hint: "刷新未来一年的开市日" },
  { key: "instruments", label: "同步证券主数据", hint: "代码、名称、指数成分" },
  { key: "index_daily", label: "同步指数日线", hint: "沪深300、中证500、上证指数" },
  { key: "daily", label: "同步个股日线", hint: "按自选股/指数成分增量拉取" },
];

export type StepStatus = "pending" | "running" | "done" | "failed" | "stopped";

export interface StepState extends SyncStep {
  status: StepStatus;
  message: string;
}

export interface JobRunResult {
  job: string;
  detail: Record<string, unknown>;
}

export type RunJob = (
  job: string,
  params?: Record<string, string | number | boolean>,
  signal?: AbortSignal,
) => Promise<JobRunResult>;

export function initialStepStates(steps: SyncStep[] = SYNC_STEPS): StepState[] {
  return steps.map((step) => ({ ...step, status: "pending", message: "等待中" }));
}

/** 从 job 返回的 detail 里提炼一行可读结果。
 *
 * 后端已在 message 字段给出中文说明（成功/部分失败/失败），优先使用；缺失时退回
 * 计数拼装，保证任何一个环节都不会只显示「完成」而没有信息量。
 */
export function stepMessage(detail: Record<string, unknown>): string {
  if (typeof detail.message === "string" && detail.message) {
    return detail.message;
  }
  const processed = Number(detail.processed ?? 0);
  const written = Number(detail.bars_written ?? 0);
  const failed = Number(detail.failed ?? 0);
  const parts = [`处理 ${processed}`, `写入 ${written}`];
  if (failed > 0) parts.push(`失败 ${failed}`);
  return parts.join("，");
}

/** 把从 ``from`` 起（含当前）的未完成环节统一标记为「已停止」。 */
function markStopped(states: StepState[], from: number): void {
  for (let index = from; index < states.length; index += 1) {
    states[index] = { ...states[index], status: "stopped", message: "已停止" };
  }
}

/** 顺序执行各环节，每步开始/结束都回调最新状态，供 UI 实时渲染。
 *
 * 某一步失败不会中断后续环节：链路是有依赖的，让用户一次看到全部环节的结果
 * （哪一步真的坏了、哪些是连带失败）比只跑一半更有用。
 *
 * 传 ``signal`` 时支持外部中止：链路只在被显式 ``abort`` 时停止，自身不会自行中断；
 * 中止后当前环节与后续环节都标记为「已停止」，已完成的结果保留。
 */
export async function runOneClickSync(
  runJob: RunJob,
  onUpdate: (steps: StepState[]) => void,
  options: { steps?: SyncStep[]; params?: Record<string, string | number | boolean>; signal?: AbortSignal } = {},
): Promise<StepState[]> {
  const steps = options.steps ?? SYNC_STEPS;
  const params = options.params ?? {};
  const signal = options.signal;
  const states = initialStepStates(steps);
  const emit = () => onUpdate(states.map((state) => ({ ...state })));

  emit();
  for (let index = 0; index < states.length; index += 1) {
    // 上一步结束后才收到停止请求：不再启动新环节。
    if (signal?.aborted) {
      markStopped(states, index);
      emit();
      return states;
    }
    states[index] = { ...states[index], status: "running", message: "进行中…" };
    emit();
    try {
      const result = await runJob(states[index].key, params, signal);
      const detail = result?.detail ?? {};
      const failed = Number(detail.failed ?? 0);
      states[index] = {
        ...states[index],
        status: failed > 0 ? "failed" : "done",
        message: stepMessage(detail),
      };
    } catch (error) {
      // 中止导致的请求失败不算真的失败：整条链路标为已停止。
      if (signal?.aborted) {
        markStopped(states, index);
        emit();
        return states;
      }
      states[index] = { ...states[index], status: "failed", message: `失败：${(error as Error).message}` };
    }
    emit();
  }
  return states;
}
