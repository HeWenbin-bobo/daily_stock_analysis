/// <reference types="vitest/globals" />
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import { KLineChart } from './KLineChart';
import type { KLineData } from '../types';

// mock lightweight-charts, 验证 applyOptions 在 market 切换时被调用
const spies = vi.hoisted(() => ({
  setData: vi.fn(),
  addCandlestickSeries: vi.fn(),
  applyOptions: vi.fn(),
  remove: vi.fn(),
  setMarkers: vi.fn(),
  createPriceLine: vi.fn(),
  removePriceLine: vi.fn(),
  createChart: vi.fn(),
}));

vi.mock('lightweight-charts', () => {
  const candleSeries = {
    setData: (...args: unknown[]) => spies.setData('candle', ...args),
    applyOptions: (...args: unknown[]) => spies.applyOptions('candle', ...args),
    setMarkers: (...args: unknown[]) => spies.setMarkers(...args),
    createPriceLine: (...args: unknown[]) => spies.createPriceLine(...args),
    removePriceLine: (...args: unknown[]) => spies.removePriceLine(...args),
  };
  const histSeries = {
    setData: (...args: unknown[]) => spies.setData('histogram', ...args),
    applyOptions: (...args: unknown[]) => spies.applyOptions('histogram', ...args),
  };
  const lineSeries = {
    setData: (...args: unknown[]) => spies.setData('line', ...args),
    applyOptions: () => {},
  };
  const chart = {
    addCandlestickSeries: (...args: unknown[]) => {
      spies.addCandlestickSeries(...args);
      return candleSeries;
    },
    addHistogramSeries: () => histSeries,
    addLineSeries: () => lineSeries,
    remove: () => spies.remove(),
    applyOptions: () => {},
    priceScale: () => ({ applyOptions: () => {} }),
  };
  spies.createChart.mockReturnValue(chart);
  return {
    createChart: spies.createChart,
  };
});

// mock matchMedia + ResizeObserver (lightweight-charts 需要)
beforeEach(() => {
  spies.setData.mockClear();
  spies.addCandlestickSeries.mockClear();
  spies.applyOptions.mockClear();
  spies.remove.mockClear();
  spies.setMarkers.mockClear();
  spies.createPriceLine.mockClear();
  spies.removePriceLine.mockClear();
  spies.createChart.mockClear();
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
  global.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver;
});

afterEach(() => {
  cleanup();
});

const sampleBars: KLineData[] = Array.from({ length: 30 }, (_, i) => ({
  date: `2026-01-${(i + 1).toString().padStart(2, '0')}`,
  open: 100 + i,
  high: 105 + i,
  low: 99 + i,
  close: 104 + i,
  volume: 1e6,
  amount: 0,
  change_percent: 0,
}));

describe('KLineChart', () => {
  it('renders without crashing', () => {
    const { container } = render(
      <KLineChart data={sampleBars} market="cn" />
    );
    // 应该创建一个 chart container
    expect(container.firstChild).toBeTruthy();
  });

  it('renders with empty data', () => {
    const { container } = render(<KLineChart data={[]} market="cn" />);
    expect(container.firstChild).toBeTruthy();
  });

  it('renders with US market', () => {
    const { container } = render(
      <KLineChart data={sampleBars} market="us" />
    );
    expect(container.firstChild).toBeTruthy();
  });

  it('renders with HK market (red-up convention)', () => {
    // HK uses the same color convention as CN (red up, green down)
    const { container } = render(
      <KLineChart data={sampleBars} market="hk" />
    );
    expect(container.firstChild).toBeTruthy();
    // 验证 addCandlestickSeries 用的是 CN/HK 的红色 (与 US 的绿色相反)
    const opts = spies.addCandlestickSeries.mock.calls[0]?.[0] as {
      upColor?: string;
    };
    expect(opts?.upColor).toBe('#ef4444'); // red for hk
  });

  it('renders with segments and year high', () => {
    const segments = [
      {
        type: 'decline' as const,
        from_date: '2026-01-10',
        to_date: '2026-01-15',
        start_price: 110,
        end_price: 105,
        change_pct: -4.55,
        from_peak: true,
      },
    ];
    const { container } = render(
      <KLineChart
        data={sampleBars}
        market="cn"
        segments={segments}
        yearHigh={{ date: '2026-01-10', price: 110 }}
      />
    );
    expect(container.firstChild).toBeTruthy();
  });

  it('renders with BOLL data', () => {
    const boll = {
      mid: sampleBars.map((b, i) => ({ time: b.date, value: 100 + i })),
      upper: sampleBars.map((b, i) => ({ time: b.date, value: 110 + i })),
      lower: sampleBars.map((b, i) => ({ time: b.date, value: 90 + i })),
    };
    const { container } = render(
      <KLineChart data={sampleBars} market="cn" boll={boll} />
    );
    expect(container.firstChild).toBeTruthy();
  });

  it('renders all data without errors', () => {
    // 验证大量数据也不崩 — 用连续递增日期
    const baseDate = new Date('2025-01-01').getTime();
    const bigData = Array.from({ length: 200 }, (_, i) => {
      const ts = baseDate + i * 24 * 60 * 60 * 1000;
      const date = new Date(ts).toISOString().slice(0, 10);
      return {
        date,
        open: 100 + i,
        high: 105 + i,
        low: 99 + i,
        close: 104 + i,
        volume: 1e6,
        amount: 0,
        change_percent: 0,
      };
    });
    const { container } = render(<KLineChart data={bigData} market="cn" />);
    expect(container.firstChild).toBeTruthy();
  });

  describe('market-driven candle colors', () => {
    it('CN defaults: red upColor when adding initial series', () => {
      render(<KLineChart data={sampleBars} market="cn" />);
      const opts = spies.addCandlestickSeries.mock.calls[0]?.[0] as {
        upColor?: string;
      };
      expect(opts?.upColor).toBe('#ef4444');
    });

    it('US defaults: green upColor when adding initial series', () => {
      render(<KLineChart data={sampleBars} market="us" />);
      const opts = spies.addCandlestickSeries.mock.calls[0]?.[0] as {
        upColor?: string;
      };
      expect(opts?.upColor).toBe('#10b981');
    });

    it('switching market from CN to US calls applyOptions on candle series (no chart rebuild)', () => {
      const { rerender } = render(<KLineChart data={sampleBars} market="cn" />);
      const initialCreates = spies.createChart.mock.calls.length;
      const initialCandleApply = spies.applyOptions.mock.calls.filter(
        (c) => c[0] === 'candle'
      ).length;
      expect(initialCreates).toBe(1);

      rerender(<KLineChart data={sampleBars} market="us" />);

      // 图表不应该重建
      expect(spies.createChart.mock.calls.length).toBe(initialCreates);
      // 蜡烛图 series 应触发 applyOptions 以应用新颜色
      const candleApplyCalls = spies.applyOptions.mock.calls.filter(
        (c) => c[0] === 'candle'
      );
      expect(candleApplyCalls.length).toBeGreaterThan(initialCandleApply);

      const lastCandleOpts = candleApplyCalls[candleApplyCalls.length - 1][1] as {
        upColor?: string;
      };
      expect(lastCandleOpts.upColor).toBe('#10b981'); // US green
    });

    it('disposes chart and resets refs on unmount', () => {
      const { unmount } = render(
        <KLineChart data={sampleBars} market="cn" segments={[]} yearHigh={null} />
      );
      expect(spies.createChart).toHaveBeenCalled();
      expect(spies.remove).not.toHaveBeenCalled();
      unmount();
      expect(spies.remove).toHaveBeenCalledTimes(1);
    });
  });
});
