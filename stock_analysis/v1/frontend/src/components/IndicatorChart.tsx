/**
 * MACD/KDJ 副图组件, 在 K线图下方展示指标线或线+直方图组合。
 * 通过 ariaLabelSuffix prop 为屏幕阅读器提供指标类型前缀 (iter 110),
 * 独立的 ErrorBoundary 隔离异常 (iter 119), 主题跟随 MutationObserver 实时切换。
 */
import { memo, useEffect, useRef } from 'react';
import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  type Time,
} from 'lightweight-charts';
import type { IndicatorPoint } from '../types';
import {
  DARK_BG,
  LIGHT_BG,
  DARK_TEXT,
  LIGHT_TEXT,
  DARK_GRID,
  LIGHT_GRID,
  DARK_BORDER,
  LIGHT_BORDER,
  applyChartTheme,
  isDarkTheme,
} from '../utils/chartTheme';

interface Props {
  /** 主线数据 (MACD=DIF, KDJ=K 等) */
  data: Array<{ time: string; value: number }>;
  /** 主线颜色 */
  color: string;
  /** 第二条线 (MACD=DEA, KDJ=D 等) */
  secondLine?: { data: IndicatorPoint[]; color: string };
  /** MACD 柱状图数据 - 与 data 同一时间轴; 提供时叠加在主/副线下方 */
  histogramData?: IndicatorPoint[];
  /** MACD 柱状图颜色数组 (可选, 不提供则按 value 正负自动着色) */
  histogramColors?: string[];
  height?: number;
  // iter 110: aria-label suffix — 之前所有 IndicatorChart 都静态写死 "指标图表, 共 N 个数据点",
  // 屏幕阅读器无法分辨是 MACD / KDJ / BOLL。父组件传入 "MACD" / "KDJ" 后, label 变为
  // "MACD 指标图表, 共 N 个数据点", 与 KLineChart 的 stock_name + period 模式对齐。
  ariaLabelSuffix?: string;
}

function sortByTime<T extends { time: string }>(arr: T[]): T[] {
  // backend compute_indicator 按日期升序返回, 但客户端调用者不能依赖这点。
  // 防御性排序: 只在检测到乱序时复制+排序, 避免稳态下每次 render 都分配新数组 (iter 63 perf)
  let outOfOrder = false;
  for (let i = 1; i < arr.length; i++) {
    const prev = arr[i - 1];
    const cur = arr[i];
    if (prev && cur && cur.time < prev.time) {
      outOfOrder = true;
      break;
    }
  }
  if (!outOfOrder) return arr;
  const out = arr.slice();
  out.sort((a, b) => a.time.localeCompare(b.time));
  return out;
}

/** 副图组件: 在主 K线图下方显示 MACD / KDJ 等指标。
 *
 * 用 lightweight-charts 的 createChart 实现, 不共享主图(简化)。
 * 支持单线/双线, 可选叠加柱状图(MACD hist)。
 *
 * 设计要点:
 * - 仅在颜色相关配置变化时重建图表
 * - data/secondLine/histogramData 等高频变化通过 setData 增量更新已存在 series, 避免抖动
 * - MACD 模式可同时显示 DIF / DEA / HIST (通过 histogramData + data + secondLine)
 */
// React.memo 避免 BOLL/MACD/KDJ 任一指标更新都重渲染其它指标图 (iter 63 perf)
export const IndicatorChart = memo(function IndicatorChart({
  data,
  color,
  secondLine,
  histogramData,
  histogramColors,
  height = 140,
  ariaLabelSuffix,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const mainSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const secondSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const histSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const styleModeRef = useRef<'histogram-only' | 'lines' | null>(null);

  // 决定呈现模式:
  // - histogramData 提供 => lines + histogram 同时显示 (MACD 期望)
  // - 仅 histogramColors 提供 (向后兼容) => 仅 histogram, data 即为柱值
  // - 其他 => 仅线图
  const hasHistogram = !!histogramData;
  const histogramOnly = !hasHistogram && !!histogramColors;
  const targetMode: 'histogram-only' | 'lines' =
    histogramOnly ? 'histogram-only' : 'lines';

  // iter 82: theme constants + helpers moved to utils/chartTheme.ts

  // 只在结构性配置变化时重建图表
  useEffect(() => {
    if (!containerRef.current) return;
    const dark = isDarkTheme();
    const chart = createChart(containerRef.current, {
      width: containerRef.current.clientWidth,
      height,
      layout: { background: { color: dark ? DARK_BG : LIGHT_BG }, textColor: dark ? DARK_TEXT : LIGHT_TEXT },
      grid: { vertLines: { color: dark ? DARK_GRID : LIGHT_GRID }, horzLines: { color: dark ? DARK_GRID : LIGHT_GRID } },
      timeScale: { visible: false, borderVisible: false },
      rightPriceScale: { borderColor: dark ? DARK_BORDER : LIGHT_BORDER },
      crosshair: { mode: 1 },
    });
    chartRef.current = chart;

    if (targetMode === 'histogram-only') {
      const histSeries = chart.addHistogramSeries({
        priceFormat: { type: 'price' },
        priceLineVisible: false,
      });
      histSeriesRef.current = histSeries;
    } else {
      const mainSeries = chart.addLineSeries({
        color,
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      mainSeriesRef.current = mainSeries;

      if (secondLine) {
        const secondSeries = chart.addLineSeries({
          color: secondLine.color,
          lineWidth: 1,
          priceLineVisible: false,
          lastValueVisible: false,
        });
        secondSeriesRef.current = secondSeries;
      }

      if (hasHistogram) {
        const histSeries = chart.addHistogramSeries({
          priceFormat: { type: 'price' },
          priceLineVisible: false,
        });
        histSeriesRef.current = histSeries;
      }
    }

    styleModeRef.current = targetMode;

    const ro = new ResizeObserver(() => {
      if (containerRef.current && chartRef.current) {
        chartRef.current.applyOptions({
          width: containerRef.current.clientWidth,
          height,
        });
      }
    });
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      mainSeriesRef.current = null;
      secondSeriesRef.current = null;
      histSeriesRef.current = null;
      styleModeRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
    // iter 94/104: 显式说明抑制理由 —
    //   - 触发条件: 只在 color/secondLine/secondLine?.color/targetMode/height 变化时重建
    //   - 安全性: data 由下方 data effect 推 setData(); mainSeries/secondSeries/histSeries
    //     实例引用稳定, close/MA/EMA 等 series 通过实例引用复用
    //   - 风险: 若 init deps 加 data 会导致 series 频繁重建 (~30ms 抖动)
    //   - 替代方案: useEvent / ref pattern 会增加复杂度, 当前 effect 分离策略已足够
    // Effect 故意只在 color/secondLine/targetMode/height 变化时重建 chart;
    // data 更新走下方第二个 effect, 避免每次 tick 都重建 series。
  // iter 113: deps 加 secondLine?.data — 之前漏了, 当 secondLine 引用稳定但
    // .data 数组新引用 (e.g. DEA 重新计算结果), init effect 不触发但下方 data
    // effect (175 行下) 会用 stale secondSeriesRef 调 setData()。
    // 实际影响有限 (下方 data effect 会同步), 但补齐 deps 防止下次重构时埋雷。
  }, [color, secondLine, secondLine?.color, secondLine?.data, targetMode, height]);

  // iter 81: 主题切换实时跟随 (同 KLineChart) — MutationObserver 监听 html.dark 类
  // iter 96: deps 改为 [] — targetMode 改变时会触发 init effect 重建 chartRef.current,
  // 但 applyChartTheme 在回调里读取 chartRef.current, 是当前值而非闭包值,
  // 所以 Observer 只挂一次即可, 避免 targetMode 每次变化都 disconnect/reconnect。
  useEffect(() => {
    if (!chartRef.current) return;
    const target = document.documentElement;
    const mo = new MutationObserver(() => applyChartTheme(chartRef.current));
    mo.observe(target, { attributes: true, attributeFilter: ['class'] });
    return () => mo.disconnect();
  }, []);

  // 数据更新: 把 data / secondLine / histogramData 推送到现有 series
  // iter 73: chartRef.current 守卫 — mode 切换时 init effect 同步清理 null 化所有 refs,
  // 但 React 18 update queue 中 data effect 可能晚一拍执行, 没有守卫会读 null 静默 no-op
  useEffect(() => {
    if (!chartRef.current) return;
    if (styleModeRef.current === 'histogram-only') {
      if (histSeriesRef.current) {
        const sorted = sortByTime(data);
        histSeriesRef.current.setData(
          sorted.map((p, i) => ({
            time: p.time as Time,
            value: p.value,
            color: histogramColors?.[i] ?? color,
          }))
        );
      }
      return;
    }

    if (styleModeRef.current === 'lines') {
      if (mainSeriesRef.current) {
        const sorted = sortByTime(data);
        mainSeriesRef.current.setData(
          sorted.map((p) => ({ time: p.time as Time, value: p.value }))
        );
      }
      if (secondSeriesRef.current && secondLine) {
        const sorted = sortByTime(secondLine.data);
        secondSeriesRef.current.setData(
          sorted.map((p) => ({ time: p.time as Time, value: p.value }))
        );
      } else if (secondSeriesRef.current && !secondLine) {
        secondSeriesRef.current.setData([]);
      }
      if (histSeriesRef.current && hasHistogram) {
        const sorted = sortByTime(histogramData!);
        histSeriesRef.current.setData(
          sorted.map((p, i) => ({
            time: p.time as Time,
            value: p.value,
            color:
              histogramColors?.[i] ??
              (p.value >= 0 ? '#10b981cc' : '#ef4444cc'),
          }))
        );
      }
    }
  }, [data, secondLine, histogramData, histogramColors, color, hasHistogram]);

  return (
    <div
      ref={containerRef}
      style={{ width: '100%', height }}
      role="img"
      // iter 110: aria-label 加 indicator 类型前缀 — "MACD 指标图表, 共 N 个数据点" / "KDJ ..."
      aria-label={`${ariaLabelSuffix ?? "指标"}图表, 共 ${data.length} 个数据点`}
    />
  );
});
