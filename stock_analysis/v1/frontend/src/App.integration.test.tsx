/// <reference types="vitest/globals" />
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, cleanup, waitFor, fireEvent, act } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import App from './App';
import { useAppStore } from './hooks/useAppStore';

// ---------------------------------------------------------------------------
// Mock matchMedia (lightweight-charts needs it in jsdom)
// ---------------------------------------------------------------------------
Object.defineProperty(window, 'matchMedia', {
  writable: true,
  value: vi.fn().mockImplementation((_query: string) => ({
    matches: false,
    media: _query,
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

// ---------------------------------------------------------------------------
// Shared mock API functions
// ---------------------------------------------------------------------------
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
  fetchIndicator: (code: string, indicator: string, period: string, days: number, signal?: AbortSignal) =>
    mockFetchIndicator(code, indicator, period, days, signal),
  fetchLLMStatus: (signal?: AbortSignal) => mockFetchLLMStatus(signal),
  fetchLLMInsight: (code: string, period: string, days: number, signal?: AbortSignal) =>
    mockFetchLLMInsight(code, period, days, signal),
  isCancelError: (err: unknown) =>
    err instanceof Error && /abort|cancel/i.test(err.message),
}));

// ---------------------------------------------------------------------------
// Shared mock data helpers
// ---------------------------------------------------------------------------
const MOCK_HISTORY = {
  stock_code: 'AAPL',
  stock_name: 'Apple Inc.',
  market: 'us' as const,
  period: 'daily',
  data: [
    { date: '2025-01-01', open: 180, high: 185, low: 179, close: 183, volume: 50000000 },
    { date: '2025-01-02', open: 183, high: 188, low: 182, close: 186, volume: 52000000 },
  ],
  source: 'mock',
};

const MOCK_ANALYSIS = {
  stock_code: 'AAPL',
  stock_name: 'Apple Inc.',
  period: 'daily',
  year_high: { date: '2025-06-15', price: 200 },
  year_start_date: '2025-01-01',
  analysis_date: '2025-07-01',
  effective_window: 4,
  segments: [],
  summary: {
    max_decline_pct: 10,
    max_rebound_pct: 8,
    current_vs_high_pct: 5,
    current_vs_year_start_pct: 3,
    current_price: 186,
    year_start_price: 180,
    segment_count: 2,
    decline_count: 1,
    rebound_count: 1,
  },
};

const MOCK_BOLL = {
  indicator: 'BOLL',
  params: { period: 20 },
  series: {
    // iter 156: date → time — 与 backend services/indicators.py:42 一致,
    // 也与 frontend types.IndicatorPoint.time 一致。之前的 date 字段是早期
    // IndicatorPoint shape 残留, 现在统一用 time。
    mid: [{ time: '2025-01-01', value: 182 }],
    upper: [{ time: '2025-01-01', value: 188 }],
    lower: [{ time: '2025-01-01', value: 176 }],
  },
};

const MOCK_MACD = {
  indicator: 'MACD',
  params: { fast: 12, slow: 26, signal: 9 },
  series: {
    dif: [{ time: '2025-01-01', value: 0.5 }],
    dea: [{ time: '2025-01-01', value: 0.3 }],
    hist: [{ time: '2025-01-01', value: 0.2 }],
  },
};

const MOCK_KDJ = {
  indicator: 'KDJ',
  params: { n: 9, m1: 3, m2: 3 },
  series: {
    k: [{ time: '2025-01-01', value: 65 }],
    d: [{ time: '2025-01-01', value: 60 }],
    j: [{ time: '2025-01-01', value: 75 }],
  },
};

// ---------------------------------------------------------------------------
// Setup / Teardown
// ---------------------------------------------------------------------------
describe('App.integration — HIGH coverage paths', () => {
  beforeEach(() => {
    // Default: all mocks resolve normally. Individual tests override as needed.
    vi.mocked(mockFetchLLMStatus).mockResolvedValue({ available: true, provider: 'openai', source: 'mock' });
    vi.mocked(mockFetchLLMInsight).mockResolvedValue({ insight: 'Mock insight', source: 'mock' });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    vi.restoreAllMocks?.();
    localStorage.clear();
    useAppStore.setState({
      selectedStock: null,
      period: 'daily',
      days: 365,
    });
  });

  // -------------------------------------------------------------------------
  // 1. Online event listener refresh (App.tsx:163-171)
  // -------------------------------------------------------------------------
  describe('online event listener refresh', () => {
    it('triggers refresh when browser fires online event', async () => {
      mockFetchHistory.mockResolvedValue(MOCK_HISTORY);
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockResolvedValue(MOCK_BOLL);

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => expect(mockFetchHistory).toHaveBeenCalled());

      mockFetchHistory.mockClear();
      fireEvent(window, new Event('online'));

      await waitFor(() => expect(mockFetchHistory).toHaveBeenCalled());
    });

    it('refresh uses current selectedStock when online event fires', async () => {
      mockFetchHistory.mockImplementation((code: string) =>
        Promise.resolve({ ...MOCK_HISTORY, stock_code: code }),
      );
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockResolvedValue(MOCK_BOLL);

      render(<App />);
      fireEvent.click(screen.getByText('Tesla (TSLA)'));

      await waitFor(() =>
        expect(mockFetchHistory).toHaveBeenCalledWith('TSLA', 'daily', 365, expect.anything()),
      );

      mockFetchHistory.mockClear();
      fireEvent(window, new Event('online'));

      await waitFor(() =>
        expect(mockFetchHistory).toHaveBeenCalledWith('TSLA', 'daily', 365, expect.anything()),
      );
    });
  });

  // -------------------------------------------------------------------------
  // 2. Stale response race condition (App.tsx:68, 74)
  // -------------------------------------------------------------------------
  describe('stale response race condition (reqTokenRef)', () => {
    it('old slow fetchHistory response does not overwrite newer state', async () => {
      let resolveSlow: (v: unknown) => void;
      let resolveFast: (v: unknown) => void;

      // Slow response for AAPL
      mockFetchHistory.mockImplementation(
        () => new Promise((resolve) => { resolveSlow = resolve; }),
      );
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockResolvedValue(MOCK_BOLL);

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => expect(mockFetchHistory).toHaveBeenCalled());

      // Switch to TSLA by setting store state (quick-pick buttons hidden after selection)
      mockFetchHistory.mockImplementation(
        () => new Promise((resolve) => { resolveFast = resolve; }),
      );
      useAppStore.setState({ selectedStock: { code: 'TSLA', name: 'Tesla', market: 'us' as const } });

      await waitFor(() =>
        expect(mockFetchHistory.mock.calls[mockFetchHistory.mock.calls.length - 1][0]).toBe('TSLA'),
      );

      // Resolve TSLA first
      act(() => { resolveFast!({ ...MOCK_HISTORY, stock_code: 'TSLA', stock_name: 'Tesla' }); });

      await waitFor(() => expect(screen.getByText('Tesla')).toBeInTheDocument());

      // Now resolve the stale AAPL response — it must NOT update display to "Apple"
      act(() => { resolveSlow!({ ...MOCK_HISTORY, stock_code: 'AAPL', stock_name: 'Apple Inc.' }); });

      expect(screen.queryByText('Apple Inc.')).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 2b. iter 161: 股票切换加载中不显示上一只股票的旧统计 (副标题)
  // -------------------------------------------------------------------------
  describe('subtitle stale stats during stock switch (iter 161)', () => {
    it('shows 加载中 instead of previous stock bar count while new stock loads', async () => {
      mockFetchHistory.mockResolvedValue(MOCK_HISTORY); // AAPL 快速返回
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockResolvedValue(MOCK_BOLL);

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      // AAPL 加载完成 → 副标题显示 "2 根 K线 · 来源 mock"
      await waitFor(() => expect(screen.getByText(/2 根 K线/)).toBeInTheDocument());
      expect(screen.getByText(/来源 mock/)).toBeInTheDocument();

      // 切换到 TSLA, fetchHistory 挂起 (loadingHistory=true)
      let resolveTsla: (v: unknown) => void;
      mockFetchHistory.mockImplementation(
        () => new Promise((resolve) => { resolveTsla = resolve; }),
      );
      useAppStore.setState({ selectedStock: { code: 'TSLA', name: 'Tesla', market: 'us' as const } });

      // 加载中: 显示 "加载中...", 不显示旧 AAPL 的 "2 根 K线 · 来源 mock"
      await waitFor(() => expect(screen.getByText(/加载中/)).toBeInTheDocument());
      expect(screen.queryByText(/2 根 K线/)).not.toBeInTheDocument();
      expect(screen.queryByText(/来源 mock/)).not.toBeInTheDocument();

      // 解析 TSLA → 显示新股票统计
      act(() => { resolveTsla!({ ...MOCK_HISTORY, stock_code: 'TSLA', stock_name: 'Tesla' }); });
      await waitFor(() => expect(screen.getByText(/2 根 K线/)).toBeInTheDocument());
      expect(screen.queryByText(/加载中/)).not.toBeInTheDocument();
    });
  });

  // -------------------------------------------------------------------------
  // 3. toggleIndicator aria-pressed (App.tsx:476-496)
  // -------------------------------------------------------------------------
  describe('indicator toggle buttons aria-pressed', () => {
    beforeEach(async () => {
      mockFetchHistory.mockResolvedValue(MOCK_HISTORY);
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockImplementation((_code: string, indicator: string) => {
        if (indicator === 'BOLL') return Promise.resolve(MOCK_BOLL);
        if (indicator === 'MACD') return Promise.resolve(MOCK_MACD);
        if (indicator === 'KDJ') return Promise.resolve(MOCK_KDJ);
        return Promise.resolve(MOCK_BOLL);
      });

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => expect(mockFetchHistory).toHaveBeenCalled());
    });

    it('BOLL button has aria-pressed=false initially when showIndicators.boll is false', () => {
      const bollBtn = screen.getByRole('button', { name: /显示 BOLL 指标/ });
      expect(bollBtn).toHaveAttribute('aria-pressed', 'false');
    });

    it('MACD button has aria-pressed=true initially because macd is default-on', () => {
      const macdBtn = screen.getByRole('button', { name: /隐藏 MACD 指标/ });
      expect(macdBtn).toHaveAttribute('aria-pressed', 'true');
    });

    it('clicking BOLL toggle changes aria-pressed from false to true', async () => {
      const bollBtn = screen.getByRole('button', { name: /显示 BOLL 指标/ });
      expect(bollBtn).toHaveAttribute('aria-pressed', 'false');

      fireEvent.click(bollBtn);

      await waitFor(() => {
        const toggledBtn = screen.getByRole('button', { name: /隐藏 BOLL 指标/ });
        expect(toggledBtn).toHaveAttribute('aria-pressed', 'true');
      });
    });

    it('clicking MACD toggle changes aria-pressed from true to false', async () => {
      const macdBtn = screen.getByRole('button', { name: /隐藏 MACD 指标/ });
      expect(macdBtn).toHaveAttribute('aria-pressed', 'true');

      fireEvent.click(macdBtn);

      await waitFor(() => {
        const toggledBtn = screen.getByRole('button', { name: /显示 MACD 指标/ });
        expect(toggledBtn).toHaveAttribute('aria-pressed', 'false');
      });
    });

    it('toggling BOLL off then on cycles aria-pressed correctly', async () => {
      const bollBtn = screen.getByRole('button', { name: /显示 BOLL 指标/ });
      expect(bollBtn).toHaveAttribute('aria-pressed', 'false');

      fireEvent.click(bollBtn);
      await waitFor(() => {
        expect(screen.getByRole('button', { name: /隐藏 BOLL 指标/ })).toHaveAttribute('aria-pressed', 'true');
      });

      fireEvent.click(screen.getByRole('button', { name: /隐藏 BOLL 指标/ }));
      await waitFor(() => {
        expect(screen.getByRole('button', { name: /显示 BOLL 指标/ })).toHaveAttribute('aria-pressed', 'false');
      });
    });
  });

  // -------------------------------------------------------------------------
  // 4. Watchlist star add/remove (App.tsx:332-354)
  // -------------------------------------------------------------------------
  describe('watchlist star add/remove', () => {
    beforeEach(async () => {
      mockFetchHistory.mockResolvedValue(MOCK_HISTORY);
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockResolvedValue(MOCK_BOLL);

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => expect(mockFetchHistory).toHaveBeenCalled());
    });

    it('star button is not filled when stock is not in watchlist', () => {
      const starBtn = screen.getByRole('button', { name: '切换自选股状态' });
      const svg = starBtn.querySelector('svg');
      expect(svg!.getAttribute('fill')).toBe('none');
      expect(svg!.classList.contains('text-yellow-400')).toBe(false);
    });

    it('clicking star adds stock to watchlist and fills the star', async () => {
      const starBtn = screen.getByRole('button', { name: '切换自选股状态' });

      fireEvent.click(starBtn);

      await waitFor(() => {
        const svg = starBtn.querySelector('svg');
        expect(svg!.getAttribute('fill')).toBe('currentColor');
        expect(svg!.classList.contains('text-yellow-400')).toBe(true);
      });
    });

    it('clicking star twice removes stock from watchlist (toggle off)', async () => {
      const starBtn = screen.getByRole('button', { name: '切换自选股状态' });

      // Add to watchlist
      fireEvent.click(starBtn);
      await waitFor(() => {
        expect(starBtn.querySelector('svg')!.getAttribute('fill')).toBe('currentColor');
      });

      // Remove from watchlist
      fireEvent.click(starBtn);
      await waitFor(() => {
        expect(starBtn.querySelector('svg')!.getAttribute('fill')).toBe('none');
      });
    });
  });

  // -------------------------------------------------------------------------
  // 5. Refresh button disabled during loading (App.tsx:368-395)
  // -------------------------------------------------------------------------
  describe('refresh button disabled during loading', () => {
    it('refresh button is enabled when not loading', async () => {
      mockFetchHistory.mockResolvedValue(MOCK_HISTORY);
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockResolvedValue(MOCK_BOLL);

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => expect(screen.getByLabelText('刷新数据')).toBeInTheDocument());
      await waitFor(() => expect(screen.getByLabelText('刷新数据')).not.toBeDisabled());
    });

    it('refresh button is disabled while fetchHistory is in flight', async () => {
      let resolve!: (v: unknown) => void;
      mockFetchHistory.mockImplementation(() => new Promise((r) => { resolve = r; }));
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockResolvedValue(MOCK_BOLL);

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => expect(screen.getByLabelText('刷新数据')).toBeDisabled());

      act(() => { resolve(MOCK_HISTORY); });
    });

    it('refresh button re-enables after fetchHistory resolves', async () => {
      let resolve!: (v: unknown) => void;
      mockFetchHistory.mockImplementation(() => new Promise((r) => { resolve = r; }));
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockResolvedValue(MOCK_BOLL);

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => expect(screen.getByLabelText('刷新数据')).toBeDisabled());

      act(() => { resolve(MOCK_HISTORY); });

      await waitFor(() => expect(screen.getByLabelText('刷新数据')).not.toBeDisabled());
    });
  });

  // -------------------------------------------------------------------------
  // 6. Error banner retry + dismiss buttons (App.tsx:421-432, 434-446)
  // -------------------------------------------------------------------------
  describe('error banner retry and dismiss buttons', () => {
    it('shows error banner when fetchHistory fails', async () => {
      mockFetchHistory.mockRejectedValue(new Error('Server error'));
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockResolvedValue(MOCK_BOLL);

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      const alert = await screen.findByRole('alert');
      expect(alert).toBeInTheDocument();
      expect(screen.getByText('重试')).toBeInTheDocument();
      expect(screen.getByLabelText('关闭错误提示')).toBeInTheDocument();
    });

    it('dismiss button clears error without refreshing', async () => {
      mockFetchHistory.mockRejectedValue(new Error('Server error'));
      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      const dismissBtn = await screen.findByLabelText('关闭错误提示');
      fireEvent.click(dismissBtn);

      await waitFor(() => {
        expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      });

      expect(mockFetchHistory).toHaveBeenCalledTimes(1);
    });

    it('retry button calls refresh after error is shown', async () => {
      mockFetchHistory
        .mockRejectedValueOnce(new Error('Server error'))
        .mockResolvedValue(MOCK_HISTORY);
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockResolvedValue(MOCK_BOLL);

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());

      mockFetchHistory.mockClear();
      fireEvent.click(screen.getByText('重试'));

      await waitFor(() => expect(mockFetchHistory).toHaveBeenCalled());
    });

    it('dismiss clears error, then refresh button refreshes with new data', async () => {
      mockFetchHistory
        .mockRejectedValueOnce(new Error('First error'))
        .mockResolvedValue(MOCK_HISTORY);
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockResolvedValue(MOCK_BOLL);

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument());

      // Dismiss first
      fireEvent.click(screen.getByLabelText('关闭错误提示'));
      await waitFor(() => expect(screen.queryByRole('alert')).not.toBeInTheDocument());

      // Then refresh
      mockFetchHistory.mockClear();
      fireEvent.click(screen.getByLabelText('刷新数据'));

      await waitFor(() => expect(mockFetchHistory).toHaveBeenCalled());
    });
  });

  // -------------------------------------------------------------------------
  // 7. BOLL/MACD/KDJ fetch error paths (App.tsx:84-140)
  // -------------------------------------------------------------------------
  describe('indicator fetch error paths', () => {
    it('BOLL fetch failure sets boll to null without crashing', async () => {
      mockFetchHistory.mockResolvedValue(MOCK_HISTORY);
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockRejectedValue(new Error('BOLL service unavailable'));

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => expect(mockFetchHistory).toHaveBeenCalled());

      // K-line and stock name should still render fine
      await waitFor(() => expect(screen.getByText('Apple Inc.')).toBeInTheDocument());

      // No error banner should appear for indicator failure (non-blocking)
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });

    it('MACD fetch failure sets macd to null without crashing', async () => {
      mockFetchHistory.mockResolvedValue(MOCK_HISTORY);
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockImplementation((_code: string, indicator: string) => {
        if (indicator === 'BOLL') return Promise.resolve(MOCK_BOLL);
        if (indicator === 'MACD') return Promise.reject(new Error('MACD service unavailable'));
        return Promise.resolve(MOCK_KDJ);
      });

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => expect(screen.getByText('Apple Inc.')).toBeInTheDocument());
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });

    it('KDJ fetch failure sets kdj to null without crashing', async () => {
      mockFetchHistory.mockResolvedValue(MOCK_HISTORY);
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockImplementation((_code: string, indicator: string) => {
        if (indicator === 'BOLL') return Promise.resolve(MOCK_BOLL);
        if (indicator === 'KDJ') return Promise.reject(new Error('KDJ service unavailable'));
        return Promise.resolve(MOCK_MACD);
      });

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => expect(screen.getByText('Apple Inc.')).toBeInTheDocument());
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });

    it('all three indicators failing simultaneously does not crash', async () => {
      mockFetchHistory.mockResolvedValue(MOCK_HISTORY);
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockRejectedValue(new Error('All indicators unavailable'));

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => expect(screen.getByText('Apple Inc.')).toBeInTheDocument());
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });

    it('stale indicator response (after stock change) is discarded', async () => {
      let resolveSlowIndicator!: (v: unknown) => void;

      // BOLL resolves slowly for AAPL
      mockFetchHistory.mockResolvedValue({ ...MOCK_HISTORY, stock_code: 'AAPL' });
      mockAnalyzePeakTrough.mockResolvedValue(MOCK_ANALYSIS);
      mockFetchIndicator.mockImplementation((_code: string, indicator: string) => {
        if (indicator === 'BOLL') {
          return new Promise((r) => { resolveSlowIndicator = r; });
        }
        return Promise.resolve(MOCK_MACD);
      });

      render(<App />);
      fireEvent.click(screen.getByText('Apple (AAPL)'));

      await waitFor(() => expect(mockFetchHistory).toHaveBeenCalled());

      // Switch to TSLA — this bumps reqTokenRef
      mockFetchHistory.mockResolvedValue({ ...MOCK_HISTORY, stock_code: 'TSLA', stock_name: 'Tesla' });
      useAppStore.setState({ selectedStock: { code: 'TSLA', name: 'Tesla', market: 'us' as const } });

      await waitFor(() =>
        expect(mockFetchHistory).toHaveBeenCalledWith('TSLA', 'daily', 365, expect.anything()),
      );

      // Now resolve the stale AAPL BOLL indicator — token mismatch should discard it
      act(() => { resolveSlowIndicator!(MOCK_BOLL); });

      // Should not crash or throw — the stale indicator update is silently dropped
      await waitFor(() => expect(screen.getByText('Tesla')).toBeInTheDocument());
    });
  });
});
