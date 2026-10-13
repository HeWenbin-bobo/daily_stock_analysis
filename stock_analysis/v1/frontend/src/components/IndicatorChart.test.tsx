/// <reference types="vitest/globals" />
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, cleanup } from '@testing-library/react';
import { IndicatorChart } from './IndicatorChart';
import type { IndicatorPoint } from '../types';

// 在 hoist 中创建 mock, 供模块工厂与测试用例共享
const spies = vi.hoisted(() => ({
  setData: vi.fn(),
  addLineSeries: vi.fn(),
  addHistogramSeries: vi.fn(),
  remove: vi.fn(),
  createChart: vi.fn(),
}));

vi.mock('lightweight-charts', () => {
  const lineSeries = {
    setData: (...args: unknown[]) => spies.setData('line', ...args),
    applyOptions: vi.fn(),
  };
  const histSeries = {
    setData: (...args: unknown[]) => spies.setData('histogram', ...args),
    applyOptions: vi.fn(),
  };
  const chart = {
    addLineSeries: (...args: unknown[]) => {
      spies.addLineSeries(...args);
      return lineSeries;
    },
    addHistogramSeries: (...args: unknown[]) => {
      spies.addHistogramSeries(...args);
      return histSeries;
    },
    remove: () => spies.remove(),
    applyOptions: vi.fn(),
    priceScale: () => ({ applyOptions: vi.fn() }),
  };
  spies.createChart.mockReturnValue(chart);
  return {
    createChart: spies.createChart,
  };
});

// lightweight-charts needs matchMedia + ResizeObserver
beforeEach(() => {
  spies.setData.mockClear();
  spies.addLineSeries.mockClear();
  spies.addHistogramSeries.mockClear();
  spies.remove.mockClear();
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

const samplePoints: IndicatorPoint[] = Array.from({ length: 30 }, (_, i) => ({
  time: `2026-01-${(i + 1).toString().padStart(2, '0')}`,
  value: 100 + i,
}));

describe('IndicatorChart', () => {
  describe('line mode', () => {
    it('renders single line without crashing', () => {
      const { container } = render(
        <IndicatorChart data={samplePoints} color="#fbbf24" />
      );
      expect(container.firstChild).toBeTruthy();
    });

    it('renders with second line', () => {
      const secondLine = {
        data: samplePoints.map((p) => ({ time: p.time, value: p.value - 10 })),
        color: '#a78bfa',
      };
      const { container } = render(
        <IndicatorChart data={samplePoints} color="#fbbf24" secondLine={secondLine} />
      );
      expect(container.firstChild).toBeTruthy();
    });

    it('renders empty data', () => {
      const { container } = render(<IndicatorChart data={[]} color="#fbbf24" />);
      expect(container.firstChild).toBeTruthy();
    });

    it('filters null values', () => {
      const dataWithNulls: IndicatorPoint[] = [
        { time: '2026-01-01', value: 100 },
        { time: '2026-01-02', value: NaN },
        { time: '2026-01-03', value: 102 },
      ];
      const { container } = render(
        <IndicatorChart data={dataWithNulls} color="#fbbf24" />
      );
      expect(container.firstChild).toBeTruthy();
    });

    it('accepts custom height', () => {
      const { container } = render(
        <IndicatorChart data={samplePoints} color="#fbbf24" height={200} />
      );
      expect(container.firstChild).toBeTruthy();
    });

    it('sorts unsorted data', () => {
      const unsorted = [
        { time: '2026-01-05', value: 105 },
        { time: '2026-01-01', value: 101 },
        { time: '2026-01-03', value: 103 },
      ];
      const { container } = render(
        <IndicatorChart data={unsorted} color="#fbbf24" />
      );
      expect(container.firstChild).toBeTruthy();
    });
  });

  describe('histogram mode', () => {
    it('renders histogram with colors', () => {
      const colors = ['#10b981cc', '#ef4444cc', '#10b981cc'];
      const { container } = render(
        <IndicatorChart
          data={samplePoints}
          color="#10b981"
          histogramColors={colors}
        />
      );
      expect(container.firstChild).toBeTruthy();
    });

    it('falls back to single color when histogram colors missing', () => {
      const { container } = render(
        <IndicatorChart data={samplePoints} color="#fbbf24" />
      );
      expect(container.firstChild).toBeTruthy();
    });

    it('handles MACD-like positive values', () => {
      const macdData: IndicatorPoint[] = [
        { time: '2026-01-01', value: 1.5 },
        { time: '2026-01-02', value: 2.3 },
        { time: '2026-01-03', value: -0.5 },
      ];
      const colors = ['#10b981cc', '#10b981cc', '#ef4444cc'];
      const { container } = render(
        <IndicatorChart data={macdData} color="#10b981" histogramColors={colors} />
      );
      expect(container.firstChild).toBeTruthy();
    });
  });

  describe('rendering robustness', () => {
    it('renders without data', () => {
      const { container } = render(<IndicatorChart data={[]} color="#fbbf24" />);
      expect(container.firstChild).toBeTruthy();
    });

    it('renders with large data set', () => {
      const baseDate = new Date('2025-01-01').getTime();
      const big = Array.from({ length: 500 }, (_, i) => {
        const ts = baseDate + i * 24 * 60 * 60 * 1000;
        return {
          time: new Date(ts).toISOString().slice(0, 10),
          value: 100 + i,
        };
      });
      const { container } = render(<IndicatorChart data={big} color="#fbbf24" />);
      expect(container.firstChild).toBeTruthy();
    });
  });

  describe('reactive update of existing series', () => {
    it('calls setData on existing main series when data prop changes (does not recreate chart)', () => {
      const data1: IndicatorPoint[] = [
        { time: '2026-01-01', value: 10 },
        { time: '2026-01-02', value: 11 },
      ];
      const data2: IndicatorPoint[] = [
        { time: '2026-01-01', value: 99 },
        { time: '2026-01-02', value: 100 },
        { time: '2026-01-03', value: 101 },
      ];
      const { rerender } = render(<IndicatorChart data={data1} color="#fbbf24" />);
      const initialCreates = spies.createChart.mock.calls.length;
      const initialSetData = spies.setData.mock.calls.length;

      rerender(<IndicatorChart data={data2} color="#fbbf24" />);

      // 图表不应该重建
      expect(spies.createChart.mock.calls.length).toBe(initialCreates);
      // setData 应该再被调用 (传播新数据)
      expect(spies.setData.mock.calls.length).toBeGreaterThan(initialSetData);
    });

    it('updates secondLine via existing series when secondLine.data changes', () => {
      const data: IndicatorPoint[] = [
        { time: '2026-01-01', value: 10 },
        { time: '2026-01-02', value: 11 },
      ];
      const second1 = { data, color: '#a78bfa' };
      const second2 = {
        data: [
          { time: '2026-01-01', value: 1 },
          { time: '2026-01-02', value: 2 },
        ],
        color: '#a78bfa',
      };
      const { rerender } = render(
        <IndicatorChart data={data} color="#fbbf24" secondLine={second1} />
      );
      rerender(
        <IndicatorChart data={data} color="#fbbf24" secondLine={second2} />
      );
      // 至少有一次 setData 针对 histogram/line 形式
      expect(spies.setData).toHaveBeenCalled();
    });
  });

  describe('MACD-style histogramData mode (new API)', () => {
    it('renders both line series AND histogram series when histogramData provided', () => {
      const dif: IndicatorPoint[] = [
        { time: '2026-01-01', value: 1.5 },
        { time: '2026-01-02', value: 2.3 },
      ];
      const dea: IndicatorPoint[] = [
        { time: '2026-01-01', value: 1.0 },
        { time: '2026-01-02', value: 1.5 },
      ];
      const hist: IndicatorPoint[] = [
        { time: '2026-01-01', value: 0.5 },
        { time: '2026-01-02', value: -0.2 },
      ];
      render(
        <IndicatorChart
          data={dif}
          color="#fbbf24"
          secondLine={{ data: dea, color: '#a78bfa' }}
          histogramData={hist}
        />
      );
      // 添加了 2 条 line + 1 条 histogram
      expect(spies.addLineSeries).toHaveBeenCalledTimes(2);
      expect(spies.addHistogramSeries).toHaveBeenCalledTimes(1);
    });

    it('histogram series receives correct data including colors', () => {
      const hist: IndicatorPoint[] = [
        { time: '2026-01-01', value: 1.0 },
        { time: '2026-01-02', value: -0.5 },
      ];
      render(
        <IndicatorChart
          data={[{ time: '2026-01-01', value: 1.5 }, { time: '2026-01-02', value: 2.0 }]}
          color="#fbbf24"
          histogramData={hist}
        />
      );
      // 找 histogram setData 调用
      const allCalls = spies.setData.mock.calls;
      const histCalls = allCalls.filter((c) => Array.isArray(c) && c[0] === 'histogram');
      expect(histCalls.length).toBeGreaterThan(0);
      const payload = histCalls[0][1] as Array<{ time: string; value: number; color: string }>;
      expect(payload.length).toBe(2);
      expect(payload[0].value).toBe(1.0);
      // value >= 0 默认绿; value < 0 默认红
      expect(payload[0].color).toMatch(/10b981/i);
      expect(payload[1].color).toMatch(/ef4444/i);
    });
  });

  describe('backward compat with histogramColors-only', () => {
    it('treats data as histogram values when only histogramColors is provided', () => {
      const data: IndicatorPoint[] = [
        { time: '2026-01-01', value: 1.0 },
        { time: '2026-01-02', value: -0.5 },
      ];
      const colors = ['#10b981cc', '#ef4444cc'];
      render(<IndicatorChart data={data} color="#10b981" histogramColors={colors} />);
      // 不应添加任何 line series
      expect(spies.addLineSeries).not.toHaveBeenCalled();
      expect(spies.addHistogramSeries).toHaveBeenCalled();
    });
  });
});
