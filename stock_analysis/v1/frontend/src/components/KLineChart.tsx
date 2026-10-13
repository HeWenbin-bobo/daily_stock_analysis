/**
 * K线图组件, 基于 lightweight-charts v4 渲染 A股/港股/美股蜡烛图及成交量。
 * 提供当前价水平线、BOLL 布林带叠加 (iter 78), ErrorBoundary 隔离
 * 初始化异常, createChart 失败时显示 role=alert 友好提示 (iter 117)。
 */
import { memo, useEffect, useMemo, useRef, useState } from 'react';
import {
  createChart,
  type IChartApi,
  type IPriceLine,
  type ISeriesApi,
  type Time,
} from 'lightweight-charts';
import type { IndicatorPoint, KLineData, PeakTroughSegment } from '../types';
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
  data: KLineData[];
  market: 'cn' | 'hk' | 'us';
  segments?: PeakTroughSegment[];
  yearHigh?: { date: string; price: number };
  /** BOLL 布林带指标 (可选, 叠加在 K线上) */
  boll?: { mid?: IndicatorPoint[]; upper?: IndicatorPoint[]; lower?: IndicatorPoint[] } | null;
}

/**
 * K线图组件 - 使用 lightweight-charts v4
 * - A股/港股: 红涨绿跌
 * - 美股: 绿涨红跌
 */
// React.memo 避免 BOLL/MACD/KDJ 三个独立 useState 落地时让图表子树白白重渲染 (iter 63 perf 优化)
export const KLineChart = memo(function KLineChart({ data, market, segments, yearHigh, boll }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleSeriesRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const ma5SeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const ma10SeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const ma20SeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const bollMidSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const bollUpperSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const bollLowerSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  // iter 72: 当前价 priceLine 句柄 — 让 effect cleanup 始终能 remove 当前实例,
  // 之前用闭包变量, 数据切换时旧 cleanup remove 旧 priceLine, 新 priceLine 永远无人 remove
  // iter 82: 用 IPriceLine — 之前 unknown 强制 unsafe cast.
  const currentPriceLineRef = useRef<IPriceLine | null>(null);
  const [ready, setReady] = useState(false);
  // iter 117: 图表初始化失败状态 — createChart 抛错时 (第三方库异常 / WebGL 不可用)
  // 之前只 DEV console.warn, finally setReady(true) 让用户看到空白图, 没有任何反馈。
  // 现在显示一行友好提示, 引导用户刷新页面。
  const [initError, setInitError] = useState<string | null>(null);

  const isUs = market === 'us';
  const upColor = isUs ? '#10b981' : '#ef4444';
  const downColor = isUs ? '#ef4444' : '#10b981';

  // 计算 MA + 波峰波谷 markers
  const { candleData, volumeData, ma5, ma10, ma20, markers } = useMemo(() => {
    const closes = data.map((d) => d.close);
    const calcMA = (period: number) =>
      closes.map((_, i) => {
        if (i < period - 1) return null;
        const slice = closes.slice(i - period + 1, i + 1);
        return slice.reduce((a, b) => a + b, 0) / period;
      });

    // 构建 markers: 用 segments 的 from_date/to_date 标记 peak/trough
    // 注: 这里用 segments 反推 — 同一日期既是上一段终点也是下一段起点, 取绝对位置标记
    const dateToBar = new Map<string, number>();
    data.forEach((d, i) => dateToBar.set(d.date, i));

    const builtMarkers: import('lightweight-charts').SeriesMarker<Time>[] = [];
    if (segments && segments.length > 0) {
      segments.forEach((seg, i) => {
        // 段终点(即 peaks/troughs 实际位置)
        const barIdx = dateToBar.get(seg.to_date);
        if (barIdx == null) return;
        const bar = data[barIdx];
        if (!bar) return; // iter 71: noUncheckedIndexedAccess 兜底 (理论上前一行已保证)
        if (seg.type === "decline") {
          // 回撤终点 = 局部低点, 标记在 bar 下方 (belowBar)
          builtMarkers.push({
            time: bar.date as Time,
            position: "belowBar",
            color: "#ef4444",
            shape: "arrowUp",  // up = 反弹买入信号
            text: `${seg.change_pct >= 0 ? "+" : ""}${seg.change_pct.toFixed(1)}%`,
            size: 1,
            id: `peak-${i}`,
          });
        } else {
          // 回弹终点 = 局部高点, 标记在 bar 上方 (aboveBar)
          builtMarkers.push({
            time: bar.date as Time,
            position: "aboveBar",
            color: "#10b981",
            shape: "arrowDown",  // down = 回撤卖出信号
            text: `${seg.change_pct >= 0 ? "+" : ""}${seg.change_pct.toFixed(1)}%`,
            size: 1,
            id: `peak-${i}`,
          });
        }
      });
    }

    // 年度高点 marker (如果还没被覆盖)
    if (yearHigh && !dateToBar.has(yearHigh.date)) {
      // date not in data, skip (unusual case)
    }

    return {
      candleData: data.map((d) => ({
        time: d.date as Time,
        open: d.open,
        high: d.high,
        low: d.low,
        close: d.close,
      })),
      volumeData: data.map((d) => ({
        time: d.date as Time,
        value: d.volume,
        color: d.close >= d.open ? (isUs ? '#10b98180' : '#ef444480') : (isUs ? '#ef444480' : '#10b98180'),
      })),
      ma5: calcMA(5),
      ma10: calcMA(10),
      ma20: calcMA(20),
      markers: builtMarkers,
    };
  }, [data, isUs, segments, yearHigh]);

  // 初始化图表
  // iter 72: try/finally + 异常路径 setReady(true) — 之前 createChart() 抛错时
  // setReady(false) 永远保持, 所有后续 data effect 都静默跳过, 用户看到空白图无任何提示
  // iter 82: theme constants + helpers moved to utils/chartTheme.ts (dedup with IndicatorChart)

  useEffect(() => {
    if (!containerRef.current) return;
    let chart: IChartApi | null = null;
    let ro: ResizeObserver | null = null;
    const dark = isDarkTheme();
    try {
      chart = createChart(containerRef.current, {
        width: containerRef.current.clientWidth,
        height: containerRef.current.clientHeight,
        layout: {
          background: { color: dark ? DARK_BG : LIGHT_BG },
          textColor: dark ? DARK_TEXT : LIGHT_TEXT,
        },
        grid: {
          vertLines: { color: dark ? DARK_GRID : LIGHT_GRID },
          horzLines: { color: dark ? DARK_GRID : LIGHT_GRID },
        },
        crosshair: { mode: 1 },
        timeScale: {
          timeVisible: true,
          secondsVisible: false,
          borderColor: dark ? DARK_BORDER : LIGHT_BORDER,
        },
        rightPriceScale: {
          borderColor: dark ? DARK_BORDER : LIGHT_BORDER,
        },
      });

      chartRef.current = chart;

      // K线系列(v4 API)
      const candleSeries = chart.addCandlestickSeries({
        upColor,
        downColor,
        borderUpColor: upColor,
        borderDownColor: downColor,
        wickUpColor: upColor,
        wickDownColor: downColor,
      });
      candleSeriesRef.current = candleSeries;

      // 成交量
      const volumeSeries = chart.addHistogramSeries({
        priceFormat: { type: 'volume' },
        priceScaleId: 'volume',
      });
      chart.priceScale('volume').applyOptions({
        scaleMargins: { top: 0.85, bottom: 0 },
      });
      volumeSeriesRef.current = volumeSeries;

      // MA 线
      const ma5Series = chart.addLineSeries({
        color: '#fbbf24',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      ma5SeriesRef.current = ma5Series;

      const ma10Series = chart.addLineSeries({
        color: '#a78bfa',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      ma10SeriesRef.current = ma10Series;

      const ma20Series = chart.addLineSeries({
        color: '#60a5fa',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
      });
      ma20SeriesRef.current = ma20Series;

      // BOLL 布林带 (3 条线, 中轨 + 上下轨)
      const bollMidSeries = chart.addLineSeries({
        color: '#f59e0b',
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: false,
        title: 'BOLL MID',
      });
      bollMidSeriesRef.current = bollMidSeries;

      const bollUpperSeries = chart.addLineSeries({
        color: '#10b981',
        lineWidth: 1,
        lineStyle: 2, // 虚线
        priceLineVisible: false,
        lastValueVisible: false,
        title: 'BOLL UPPER',
      });
      bollUpperSeriesRef.current = bollUpperSeries;

      const bollLowerSeries = chart.addLineSeries({
        color: '#ef4444',
        lineWidth: 1,
        lineStyle: 2,
        priceLineVisible: false,
        lastValueVisible: false,
        title: 'BOLL LOWER',
      });
      bollLowerSeriesRef.current = bollLowerSeries;

      // ResizeObserver
      ro = new ResizeObserver(() => {
        if (containerRef.current && chartRef.current) {
          chartRef.current.applyOptions({
            width: containerRef.current.clientWidth,
            height: containerRef.current.clientHeight,
          });
        }
      });
      ro.observe(containerRef.current);
    } catch (e) {
      // iter 72: createChart 抛错时不让 setReady 永远停在 false
      // iter 117: 同步设置 initError — 之前只 DEV log, 用户看到空白图。
      setInitError("图表初始化失败, 请刷新页面或更换浏览器");
      if (import.meta.env.DEV) {
        console.warn('KLineChart init failed:', e);
      }
    } finally {
      setReady(true);
    }

    return () => {
      if (ro) ro.disconnect();
      if (chart) chart.remove();
      chartRef.current = null;
      candleSeriesRef.current = null;
      volumeSeriesRef.current = null;
      ma5SeriesRef.current = null;
      ma10SeriesRef.current = null;
      ma20SeriesRef.current = null;
      bollMidSeriesRef.current = null;
      bollUpperSeriesRef.current = null;
      bollLowerSeriesRef.current = null;
    };
    // 系列创建时只跑一次; 颜色更新交给下方独立 effect 用 applyOptions 处理, 避免重建图表
    // iter 94/104: 显式说明抑制理由 —
    //   - 触发条件: 只在 ready/color/secondLine?.color/targetMode/height 变化时重建 series
    //   - 安全性: data 由下方 data effect 推 setData(); upColor/downColor 等通过
    //     独立 applyOptions effect 热更新 (避免重建图表, 重建 ~50ms 抖动)
    //   - 风险: 若 init deps 加 data 会在 data 变时重建图表 (用户每次切股票都闪一下)
    //   - 替代方案: useEvent / ref pattern 会增加复杂度, 当前 effect 分离策略已足够
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // iter 81: 主题切换 — 监听 html.dark 类的变化, 实时更新图表背景/网格/边框。
  // MutationObserver 监听 attribute 变化, 避免重建图表。
  useEffect(() => {
    if (!ready) return;
    const target = document.documentElement;
    const mo = new MutationObserver(() => applyChartTheme(chartRef.current));
    mo.observe(target, { attributes: true, attributeFilter: ['class'] });
    return () => mo.disconnect();
  }, [ready]);

  // 市场变化时通过 applyOptions 重新设置蜡烛颜色, 避免重建整个图表
  useEffect(() => {
    if (!ready || !candleSeriesRef.current) return;
    candleSeriesRef.current.applyOptions({
      upColor,
      downColor,
      borderUpColor: upColor,
      borderDownColor: downColor,
      wickUpColor: upColor,
      wickDownColor: downColor,
    });
    // 成交量直方图颜色也同步刷新, 与蜡烛图保持一致
    if (volumeSeriesRef.current) {
      volumeSeriesRef.current.setData(
        data.map((d) => ({
          time: d.date as Time,
          value: d.volume,
          color: d.close >= d.open ? (isUs ? '#10b98180' : '#ef444480') : (isUs ? '#ef444480' : '#10b98180'),
        }))
      );
    }
  }, [ready, isUs, upColor, downColor, data]);

  // 更新数据
  useEffect(() => {
    if (!ready) return;
    candleSeriesRef.current?.setData(candleData);
    volumeSeriesRef.current?.setData(volumeData);
    ma5SeriesRef.current?.setData(
      ma5.map((v, i) => {
        const bar = data[i];
        return v == null || !bar ? null : { time: bar.date as Time, value: v };
      })
        .filter((x): x is { time: Time; value: number } => x !== null)
    );
    ma10SeriesRef.current?.setData(
      ma10.map((v, i) => {
        const bar = data[i];
        return v == null || !bar ? null : { time: bar.date as Time, value: v };
      })
        .filter((x): x is { time: Time; value: number } => x !== null)
    );
    ma20SeriesRef.current?.setData(
      ma20.map((v, i) => {
        const bar = data[i];
        return v == null || !bar ? null : { time: bar.date as Time, value: v };
      })
        .filter((x): x is { time: Time; value: number } => x !== null)
    );
    // 在 K线上叠加波峰/波谷 markers (按 time 排序)
    // iter 72: 防御性 narrow — 缓存条目畸形 (time 非 string) 时跳过避免 localeCompare 抛错
    try {
      const sortedMarkers = [...markers]
        .filter((m): m is typeof m & { time: string } => typeof m.time === 'string')
        .sort((a, b) => a.time.localeCompare(b.time));
      candleSeriesRef.current?.setMarkers(sortedMarkers);
    } catch (e) {
      if (import.meta.env.DEV) {
        console.warn('setMarkers failed (likely data type mismatch):', e);
      }
    }

    // BOLL 布林带叠加 — iter 78: setData([]) 仅清数据, series line 仍可见.
    // 用户关闭 BOLL 时应通过 applyOptions({ visible: false }) 隐藏线条本身
    const setBollData = (points?: IndicatorPoint[]) => {
      const data = (points ?? [])
        .map((p) => ({ time: p.time as Time, value: p.value }))
        .filter((x): x is { time: Time; value: number } => x.value !== null && !isNaN(x.value))
        .sort((a, b) => (a.time as string).localeCompare(b.time as string));
      return data;
    };
    const showBoll = boll != null;
    bollMidSeriesRef.current?.applyOptions({ visible: showBoll });
    bollUpperSeriesRef.current?.applyOptions({ visible: showBoll });
    bollLowerSeriesRef.current?.applyOptions({ visible: showBoll });
    if (boll) {
      bollMidSeriesRef.current?.setData(setBollData(boll.mid));
      bollUpperSeriesRef.current?.setData(setBollData(boll.upper));
      bollLowerSeriesRef.current?.setData(setBollData(boll.lower));
    } else {
      bollMidSeriesRef.current?.setData([]);
      bollUpperSeriesRef.current?.setData([]);
      bollLowerSeriesRef.current?.setData([]);
    }
  }, [ready, candleData, volumeData, ma5, ma10, ma20, data, markers, boll]);

  // 年度最高点水平线
  useEffect(() => {
    if (!ready || !yearHigh || !candleSeriesRef.current) return;
    const series = candleSeriesRef.current;
    const priceLine = series.createPriceLine({
      price: yearHigh.price,
      color: '#fbbf24',
      // lineWidth=2 让年度高点线略粗于普通均线, 在视觉层次上更突出
      lineWidth: 2,
      lineStyle: 2,
      axisLabelVisible: true,
      title: `年度高点 ${yearHigh.price.toFixed(2)}`,
    });
    return () => {
      try {
        series.removePriceLine(priceLine);
      } catch {
        /* ignore */
      }
    };
  }, [ready, yearHigh]);

  // iter 70: 当前价水平线 — 与年度高点形成视觉对比, 让用户一眼看出当前位置
  // iter 72: 用 currentPriceLineRef 保存当前 priceLine, 避免前次 effect 的 cleanup
  // 误 remove 旧 priceLine 而新 priceLine 永远残留
  useEffect(() => {
    if (!ready || data.length === 0 || !candleSeriesRef.current) {
      currentPriceLineRef.current = null;
      return;
    }
    const series = candleSeriesRef.current;
    const lastBar = data[data.length - 1];
    if (!lastBar) return; // iter 71: noUncheckedIndexedAccess 兜底
    const currentPrice = lastBar.close;
    const priceLine = series.createPriceLine({
      price: currentPrice,
      color: '#3b82f6',
      lineWidth: 1,
      lineStyle: 3, // 点划线
      axisLabelVisible: true,
      title: `当前价 ${currentPrice.toFixed(2)}`,
    });
    const previous = currentPriceLineRef.current;
    currentPriceLineRef.current = priceLine;
    return () => {
      // 优先 remove 当前 effect 创建的; 兜底 remove 上一次 effect 残留的
      try {
        series.removePriceLine(priceLine);
      } catch {
        /* ignore */
      }
      if (previous && previous !== priceLine) {
        try {
          series.removePriceLine(previous);
        } catch {
          /* ignore */
        }
      }
    };
  }, [ready, data]);

  // iter 96: 动态 aria-label — 让屏幕阅读器知道当前 K 线数量,
  // 静态 "K线图" 太笼统。period/name 由 App 层传入时再加入 label。
  // iter 117: initError 优先渲染 — createChart 失败时 (WebGL 不可用 / 第三方库异常)
  // 给用户友好提示, 而不是空白图。role="alert" 让 SR 立即播报。
  if (initError) {
    return (
      <div
        className="kline-chart min-h-[300px] flex items-center justify-center text-slate-400 text-sm bg-panel border border-border rounded-lg"
        role="alert"
      >
        {initError}
      </div>
    );
  }
  return (
    <div
      ref={containerRef}
      className="kline-chart min-h-[300px]"
      role="img"
      aria-label={`K线图, 共 ${data.length} 根 K 线`}
    />
  );
});