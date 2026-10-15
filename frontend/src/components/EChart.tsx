/** Thin ECharts wrapper.
 *
 * ECharts owns a DOM node and must be disposed on unmount; leaking instances is
 * the usual cause of "canvas already in use" errors when navigating between pages,
 * so disposal is handled explicitly here.
 */

import { useEffect, useRef } from "react";
import * as echarts from "echarts";

interface Props {
  option: echarts.EChartsOption;
  height?: number | string;
  /** Replace the whole option instead of merging (needed when series count changes). */
  notMerge?: boolean;
}

export default function EChart({ option, height = 360, notMerge = true }: Props) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const chart = echarts.init(containerRef.current, "dark");
    chartRef.current = chart;

    const handleResize = () => chart.resize();
    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!chartRef.current) return;
    chartRef.current.setOption(option, notMerge);
  }, [option, notMerge]);

  return <div ref={containerRef} style={{ width: "100%", height }} />;
}
