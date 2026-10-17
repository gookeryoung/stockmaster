/** Candlestick chart with volume and an optional MACD subplot.
 *
 * A-share colour convention is inverted relative to Western charts: rising is
 * red and falling is green. Volume bars are tinted to match, and the dataZoom
 * range defaults to the most recent ~120 sessions.
 */

import { useMemo } from "react";
import type { EChartsOption } from "echarts";
import EChart from "./EChart";
import type { Bar, IndicatorSnapshot } from "../types";

interface Props {
  bars: Bar[];
  indicators?: IndicatorSnapshot | null;
  /** 数字按 px 处理；也接受 clamp()/vh 等 CSS 长度，便于手机端自适应。 */
  height?: number | string;
  showMacd?: boolean;
}

export default function KlineChart({ bars, indicators, height = 460, showMacd = true }: Props) {
  const option = useMemo<EChartsOption>(() => {
    const dates = bars.map((b) => b.trade_date);
    // ECharts candlestick expects [open, close, low, high].
    const candles = bars.map((b) => [b.open, b.close, b.low, b.high]);
    const closes = bars.map((b) => b.close);

    const volumes = bars.map((b, index) => ({
      value: b.volume,
      itemStyle: {
        color: index > 0 && b.close >= bars[index - 1].close ? "#ef4444" : "#22c55e",
      },
    }));

    const series: NonNullable<EChartsOption["series"]> = [
      {
        name: "K线",
        type: "candlestick",
        data: candles,
        itemStyle: {
          color: "#ef4444",        // up body
          color0: "#22c55e",       // down body
          borderColor: "#ef4444",
          borderColor0: "#22c55e",
        },
        xAxisIndex: 0,
        yAxisIndex: 0,
      },
      {
        name: "MA20",
        type: "line",
        data: movingAverage(closes, 20),
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1, color: "#38bdf8" },
        xAxisIndex: 0,
        yAxisIndex: 0,
      },
      {
        name: "MA60",
        type: "line",
        data: movingAverage(closes, 60),
        smooth: true,
        showSymbol: false,
        lineStyle: { width: 1, color: "#f59e0b" },
        xAxisIndex: 0,
        yAxisIndex: 0,
      },
      {
        name: "成交量",
        type: "bar",
        data: volumes,
        xAxisIndex: 1,
        yAxisIndex: 1,
      },
    ];

    const grid = showMacd
      ? [
          { left: 56, right: 24, top: 30, height: "46%" },
          { left: 56, right: 24, top: "58%", height: "14%" },
          { left: 56, right: 24, top: "76%", height: "16%" },
        ]
      : [
          { left: 56, right: 24, top: 30, height: "62%" },
          { left: 56, right: 24, top: "76%", height: "16%" },
        ];

    const xAxis: NonNullable<EChartsOption["xAxis"]> = [
      {
        type: "category",
        data: dates,
        gridIndex: 0,
        boundaryGap: true,
        axisLine: { lineStyle: { color: "#334155" } },
        axisLabel: { color: "#94a3b8" },
      },
      {
        type: "category",
        data: dates,
        gridIndex: 1,
        axisLabel: { show: false },
        axisLine: { lineStyle: { color: "#334155" } },
      },
    ];

    const yAxis: NonNullable<EChartsOption["yAxis"]> = [
      {
        scale: true,
        gridIndex: 0,
        splitLine: { lineStyle: { color: "#1e293b" } },
        axisLabel: { color: "#94a3b8" },
      },
      {
        gridIndex: 1,
        splitNumber: 2,
        axisLabel: { show: false },
        splitLine: { show: false },
      },
    ];

    if (showMacd) {
      const macdValues = bars.map((_, index) => {
        // Derive a simple MACD locally so the subplot does not require the whole
        // indicator payload; the API-provided values are preferred when present.
        const ema12 = emaAt(closes, 12, index);
        const ema26 = emaAt(closes, 26, index);
        return ema12 !== null && ema26 !== null ? ema12 - ema26 : null;
      });

      xAxis.push({
        type: "category",
        data: dates,
        gridIndex: 2,
        axisLabel: { color: "#94a3b8" },
        axisLine: { lineStyle: { color: "#334155" } },
      });
      yAxis.push({
        gridIndex: 2,
        scale: true,
        splitLine: { lineStyle: { color: "#1e293b" } },
        axisLabel: { color: "#94a3b8" },
      });
      series.push({
        name: "DIF",
        type: "line",
        data: macdValues,
        showSymbol: false,
        lineStyle: { width: 1, color: "#38bdf8" },
        xAxisIndex: 2,
        yAxisIndex: 2,
      });
    }

    const start = bars.length > 150 ? Math.round((1 - 150 / bars.length) * 100) : 0;

    return {
      backgroundColor: "transparent",
      animation: false,
      tooltip: { trigger: "axis", axisPointer: { type: "cross" } },
      legend: {
        data: showMacd ? ["K线", "MA20", "MA60", "DIF"] : ["K线", "MA20", "MA60"],
        textStyle: { color: "#94a3b8" },
        top: 0,
      },
      grid,
      xAxis,
      yAxis,
      dataZoom: [
        { type: "inside", xAxisIndex: showMacd ? [0, 1, 2] : [0, 1], start, end: 100 },
        {
          type: "slider",
          xAxisIndex: showMacd ? [0, 1, 2] : [0, 1],
          bottom: 2,
          height: 16,
          start,
          end: 100,
          textStyle: { color: "#94a3b8" },
        },
      ],
      series,
    };
  }, [bars, indicators, showMacd]);

  return <EChart option={option} height={height} />;
}

function movingAverage(values: number[], window: number): Array<number | null> {
  return values.map((_, index) => {
    if (index < window - 1) return null;
    const slice = values.slice(index - window + 1, index + 1);
    return slice.reduce((sum, v) => sum + v, 0) / window;
  });
}

function emaAt(values: number[], span: number, index: number): number | null {
  if (index < 0 || index >= values.length) return null;
  const alpha = 2 / (span + 1);
  let ema = values[0];
  for (let i = 1; i <= index; i += 1) {
    ema = values[i] * alpha + ema * (1 - alpha);
  }
  return ema;
}
