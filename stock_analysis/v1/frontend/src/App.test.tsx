/// <reference types="vitest/globals" />
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import App from './App';
import { useAppStore } from './hooks/useAppStore';

// Mock matchMedia for lightweight-charts (jsdom doesn't have it)
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
// Mock ResizeObserver (jsdom doesn't have it)
global.ResizeObserver = class ResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

// Mock 整个 api/client 模块
const mockSearchStocks = vi.fn();
const mockFetchHistory = vi.fn();
const mockAnalyzePeakTrough = vi.fn();
const mockFetchIndicator = vi.fn();
const mockFetchLLMStatus = vi.fn();
const mockFetchLLMInsight = vi.fn();

vi.mock('./api/client', () => ({
  searchStocks: (q: string) => mockSearchStocks(q),
  fetchHistory: (code: string, period: string, days: number, signal?: AbortSignal) =>
    mockFetchHistory(code, period, days, signal),
  analyzePeakTrough: (code: string, period: string, days: number) =>
    mockAnalyzePeakTrough(code, period, days),
  fetchIndicator: (code: string, indicator: string, period: string, days: number) =>
    mockFetchIndicator(code, indicator, period, days),
  fetchLLMStatus: () => mockFetchLLMStatus(),
  fetchLLMInsight: (code: string, period: string, days: number) =>
    mockFetchLLMInsight(code, period, days),
  isCancelError: (err: unknown) =>
    err instanceof Error && /abort|cancel/i.test(err.message),
}));

describe('App', () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    // 同步重置 store
    useAppStore.setState({
      selectedStock: null,
      period: 'daily',
      days: 365,
    });
  });

  describe('initial render', () => {
    it('renders header with title', () => {
      render(<App />);
      expect(screen.getByText(/个股 K线/)).toBeInTheDocument();
    });

    it('renders search bar', () => {
      render(<App />);
      expect(screen.getByPlaceholderText(/搜索股票/)).toBeInTheDocument();
    });

    it('renders period selector buttons', () => {
      render(<App />);
      expect(screen.getByText('日 K')).toBeInTheDocument();
      expect(screen.getByText('周 K')).toBeInTheDocument();
      expect(screen.getByText('月 K')).toBeInTheDocument();
    });

    it('shows empty state when no stock selected', () => {
      render(<App />);
      expect(screen.getByText(/开始一次分析/)).toBeInTheDocument();
    });

    it('shows quick-pick stock buttons', () => {
      render(<App />);
      expect(screen.getByText('贵州茅台 (600519)')).toBeInTheDocument();
      expect(screen.getByText('Apple (AAPL)')).toBeInTheDocument();
    });
  });

  describe('stock selection', () => {
    it('clicking quick-pick button fetches history', async () => {
      mockFetchHistory.mockResolvedValue({
        stock_code: 'AAPL', stock_name: 'Apple', market: 'us',
        period: 'daily', data: [], source: 'mock',
      });
      render(<App />);
      const aaplBtn = screen.getByText('Apple (AAPL)');
      fireEvent.click(aaplBtn);

      await waitFor(() => {
        expect(mockFetchHistory).toHaveBeenCalledWith(
          'AAPL', 'daily', 365, expect.anything()
        );
      });
    });

    it('shows mock data banner when source is mock', async () => {
      mockFetchHistory.mockResolvedValue({
        stock_code: '600519', stock_name: '贵州茅台', market: 'cn',
        period: 'daily', data: [], source: 'mock',
      });
      render(<App />);
      fireEvent.click(screen.getByText('贵州茅台 (600519)'));

      await waitFor(() => {
        expect(screen.getByText(/演示数据/)).toBeInTheDocument();
      });
    });
  });

  describe('period change', () => {
    it('clicking weekly period triggers new fetch', async () => {
      mockFetchHistory.mockResolvedValue({
        stock_code: 'AAPL', stock_name: 'Apple', market: 'us',
        period: 'daily', data: [], source: 'mock',
      });
      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => {
        expect(mockFetchHistory).toHaveBeenCalled();
      });

      // 切换周期
      fireEvent.click(screen.getByText('周 K'));

      await waitFor(() => {
        expect(mockFetchHistory).toHaveBeenCalledWith(
          'AAPL', 'weekly', 365, expect.anything()
        );
      });
    });
  });

  describe('theme toggle', () => {
    it('toggles theme on button click', () => {
      render(<App />);
      const themeBtn = screen.getByRole('button', { name: '切换主题' });
      fireEvent.click(themeBtn);
      // 点击后 html 标签应变为 'light' (默认是 dark)
      expect(document.documentElement.classList.contains('light')).toBe(true);
    });
  });

  describe('error handling', () => {
    it('shows error message when fetch fails', async () => {
      mockFetchHistory.mockRejectedValue(new Error('Network error'));
      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => {
        // iter 84: toUserMessage 对非 axios 错误一律走 fallback, 不再泄露 raw message
        expect(screen.getByText(/获取 K线失败: 操作失败, 请稍后重试/)).toBeInTheDocument();
      });
    });

    // iter 63: 错误提示必须带 role="alert" 让屏幕阅读器播报 (WCAG 4.1.3)
    it('error banner has role=alert for screen reader announcement', async () => {
      mockFetchHistory.mockRejectedValue(new Error('Server error 500'));
      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      const alert = await screen.findByRole('alert');
      expect(alert).toHaveTextContent(/获取 K线失败/);
    });
  });

  describe('AbortController + cleanup', () => {
    it('passes an AbortSignal to fetchHistory on every call', async () => {
      mockFetchHistory.mockResolvedValue({
        stock_code: 'AAPL', stock_name: 'Apple', market: 'us',
        period: 'daily', data: [], source: 'mock',
      });
      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => {
        expect(mockFetchHistory).toHaveBeenCalled();
      });
      const lastCall = mockFetchHistory.mock.calls[
        mockFetchHistory.mock.calls.length - 1
      ];
      const signal = lastCall[lastCall.length - 1];
      expect(signal).toBeDefined();
      expect(typeof (signal as { aborted?: boolean }).aborted).toBe('boolean');
    });

    it('unmounting aborts in-flight fetchHistory (signal.aborted)', async () => {
      let resolveFn!: (v: unknown) => void;
      mockFetchHistory.mockImplementation(
        () => new Promise((resolve) => {
          resolveFn = resolve;
        })
      );
      mockAnalyzePeakTrough.mockResolvedValue({
        stock_code: 'AAPL',
        stock_name: 'Apple',
        period: 'daily',
        year_high: { date: '2026-01-01', price: 100 },
        year_start_date: '2025-12-31',
        analysis_date: '2026-01-01',
        segments: [],
        summary: {
          max_decline_pct: 0,
          max_rebound_pct: 0,
          current_vs_high_pct: 0,
          current_vs_year_start_pct: 0,
          current_price: 100,
          year_start_price: 100,
          segment_count: 0,
          decline_count: 0,
          rebound_count: 0,
        },
      });
      mockFetchIndicator.mockResolvedValue({
        indicator: 'BOLL',
        params: {},
        series: { mid: [], upper: [], lower: [] },
      });

      const { unmount } = render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));
      await waitFor(() => expect(mockFetchHistory).toHaveBeenCalled());

      const lastCall = mockFetchHistory.mock.calls[
        mockFetchHistory.mock.calls.length - 1
      ];
      const signal = lastCall[lastCall.length - 1] as { aborted?: boolean };
      expect(signal.aborted).toBe(false);

      unmount();
      // 卸载后, 最后一次请求的 signal 应被 abort
      expect(signal.aborted).toBe(true);
      // 兜底 resolve 防止 dangling promise warning
      resolveFn({ stock_code: 'AAPL', data: [] });
    });
  });
});
