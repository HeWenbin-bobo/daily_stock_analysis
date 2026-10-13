/// <reference types="vitest/globals" />
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  applyChartTheme,
  DARK_BG,
  DARK_BORDER,
  DARK_GRID,
  DARK_TEXT,
  isDarkTheme,
  LIGHT_BG,
  LIGHT_BORDER,
  LIGHT_GRID,
  LIGHT_TEXT,
} from './chartTheme';

// iter 125: 补测 chartTheme light/dark 主题切换 — review 报告标注为 MEDIUM,
// 现有 0% branch 覆盖。applyChartTheme 是 KLineChart + IndicatorChart 每次 init 都调的
// 工具函数, 任何回归 (e.g. light 模式忘记切 palette) 都会让浅色背景图表变成深色文字,
// 用户完全看不见。
describe('chartTheme', () => {
  beforeEach(() => {
    // 每个测试前确保 html 默认无 .light class (暗色主题)
    document.documentElement.classList.remove('light');
  });

  afterEach(() => {
    document.documentElement.classList.remove('light');
  });

  describe('isDarkTheme', () => {
    it('returns true when html has no .light class', () => {
      document.documentElement.classList.remove('light');
      expect(isDarkTheme()).toBe(true);
    });

    it('returns false when html has .light class', () => {
      document.documentElement.classList.add('light');
      expect(isDarkTheme()).toBe(false);
    });
  });

  describe('applyChartTheme', () => {
    function makeMockChart() {
      return { applyOptions: vi.fn() };
    }

    it('applies dark palette when html has no .light class', () => {
      const chart = makeMockChart();
      applyChartTheme(chart as unknown as import('lightweight-charts').IChartApi);

      expect(chart.applyOptions).toHaveBeenCalledTimes(1);
      const opts = chart.applyOptions.mock.calls[0][0];
      expect(opts.layout.background.color).toBe(DARK_BG);
      expect(opts.layout.textColor).toBe(DARK_TEXT);
      expect(opts.grid.vertLines.color).toBe(DARK_GRID);
      expect(opts.grid.horzLines.color).toBe(DARK_GRID);
      expect(opts.timeScale.borderColor).toBe(DARK_BORDER);
    });

    it('applies light palette when html has .light class', () => {
      document.documentElement.classList.add('light');
      const chart = makeMockChart();
      applyChartTheme(chart as unknown as import('lightweight-charts').IChartApi);

      expect(chart.applyOptions).toHaveBeenCalledTimes(1);
      const opts = chart.applyOptions.mock.calls[0][0];
      expect(opts.layout.background.color).toBe(LIGHT_BG);
      expect(opts.layout.textColor).toBe(LIGHT_TEXT);
      expect(opts.grid.vertLines.color).toBe(LIGHT_GRID);
      expect(opts.grid.horzLines.color).toBe(LIGHT_GRID);
      expect(opts.timeScale.borderColor).toBe(LIGHT_BORDER);
    });

    it('early-returns when chart is null (defensive guard)', () => {
      // 不应崩溃, 也不应调用任何方法 (因为根本没有 chart)
      expect(() => applyChartTheme(null)).not.toThrow();
    });
  });
});