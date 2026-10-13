/// <reference types="vitest/globals" />
import { beforeEach, describe, expect, it, vi } from 'vitest';

// Mock axios BEFORE importing the module under test
vi.mock('axios', async (importOriginal) => {
  const actual = await importOriginal<typeof import('axios')>();
  const instance: Record<string, unknown> = {
    get: vi.fn(),
    post: vi.fn(),
    // iter 134: client.ts 现在挂了 response interceptor (X-Request-ID 关联);
    // mock 必须提供 interceptors.response.use 才能让 client.ts 模块加载成功。
    interceptors: {
      response: { use: vi.fn() },
    },
  };
  // 暴露给测试用
  (globalThis as Record<string, unknown>).__mockAxiosInstance = instance;
  return {
    ...actual,
    default: {
      ...actual.default,
      create: vi.fn(() => instance),
      isCancel: vi.fn(
        (err: unknown) =>
          err !== null &&
          typeof err === 'object' &&
          (err as { name?: string }).name === 'CanceledError'
      ),
    },
  };
});

// Import after mock
import axios from 'axios';
import {
  analyzePeakTrough,
  fetchHistory,
  fetchIndicator,
  fetchLLMInsight,
  fetchLLMStatus,
  isCancelError,
  searchStocks,
  withRetry,
} from './client';

const mockInstance = (): { get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn> } =>
  (globalThis as Record<string, unknown>).__mockAxiosInstance as {
    get: ReturnType<typeof vi.fn>;
    post: ReturnType<typeof vi.fn>;
  };

beforeEach(() => {
  const m = mockInstance();
  m.get.mockReset();
  m.post.mockReset();
});

describe('api/client.ts', () => {
  describe('searchStocks', () => {
    it('returns empty array for empty query without making request', async () => {
      const result = await searchStocks('');
      expect(result).toEqual([]);
      expect(mockInstance().get).not.toHaveBeenCalled();
    });

    it('returns empty array for whitespace query', async () => {
      const result = await searchStocks('   ');
      expect(result).toEqual([]);
      expect(mockInstance().get).not.toHaveBeenCalled();
    });

    it('returns items on success', async () => {
      const mockData = {
        items: [
          { code: '600519', name: '贵州茅台', market: 'cn' },
          { code: 'AAPL', name: 'Apple', market: 'us' },
        ],
      };
      mockInstance().get.mockResolvedValue({ data: mockData });

      const result = await searchStocks('茅台');
      expect(result).toEqual(mockData.items);
      expect(mockInstance().get).toHaveBeenCalledWith(
        '/stocks/search',
        expect.objectContaining({ params: { q: '茅台' } })
      );
    });

    it('passes AbortSignal when provided', async () => {
      mockInstance().get.mockResolvedValue({ data: { items: [] } });
      const ctrl = new AbortController();
      await searchStocks('AAPL', ctrl.signal);
      expect(mockInstance().get).toHaveBeenCalledWith(
        '/stocks/search',
        expect.objectContaining({ signal: ctrl.signal })
      );
    });
  });

  describe('fetchHistory', () => {
    it('calls correct endpoint with period + days params', async () => {
      mockInstance().get.mockResolvedValue({
        data: { stock_code: 'X', stock_name: 'X', market: 'cn', period: 'daily', data: [], source: 'mock' },
      });

      await fetchHistory('600519', 'weekly', 180);

      expect(mockInstance().get).toHaveBeenCalledWith(
        '/stocks/600519/history',
        expect.objectContaining({ params: { period: 'weekly', days: 180 } })
      );
    });

    it('uses default days=365 when not specified', async () => {
      mockInstance().get.mockResolvedValue({ data: {} });
      await fetchHistory('AAPL', 'daily');
      const callArgs = mockInstance().get.mock.calls[0][1] as { params: { days: number } };
      expect(callArgs.params.days).toBe(365);
    });
  });

  describe('fetchIndicator', () => {
    it('routes to correct indicator endpoint', async () => {
      mockInstance().get.mockResolvedValue({
        data: { indicator: 'MACD', params: {}, series: {} },
      });

      await fetchIndicator('600519', 'BOLL', 'daily', 90);

      expect(mockInstance().get).toHaveBeenCalledWith(
        '/stocks/600519/indicators/BOLL',
        expect.objectContaining({ params: { period: 'daily', days: 90 } })
      );
    });
  });

  describe('fetchLLMInsight', () => {
    it('POSTs to /analysis/llm-insight with body', async () => {
      mockInstance().post.mockResolvedValue({
        data: { provider: 'MiniMax', model: 'X', text: '分析', source: 'cc_switch' },
      });

      await fetchLLMInsight('600519', 'daily', 365, 'extra question');

      expect(mockInstance().post).toHaveBeenCalledWith(
        '/analysis/llm-insight',
        expect.objectContaining({
          stock_code: '600519',
          period: 'daily',
          days: 365,
          extra_prompt: 'extra question',
        }),
        expect.any(Object)
      );
    });

    it('handles no extra_prompt', async () => {
      mockInstance().post.mockResolvedValue({ data: {} });
      await fetchLLMInsight('AAPL', 'weekly', 60);
      const body = mockInstance().post.mock.calls[0][1] as { extra_prompt?: string };
      expect(body.extra_prompt).toBeUndefined();
    });
  });

  describe('fetchLLMStatus', () => {
    it('GETs /analysis/llm-status', async () => {
      mockInstance().get.mockResolvedValue({
        data: {
          available: true,
          source: 'cc_switch',
          provider: 'MiniMax',
          model: null,
          base_url: null,
          app_type: null,
          all_providers: [],
        },
      });
      await fetchLLMStatus();
      expect(mockInstance().get).toHaveBeenCalledWith(
        '/analysis/llm-status',
        expect.any(Object)
      );
    });
  });

  describe('isCancelError', () => {
    it('returns true for axios.CanceledError', () => {
      const cancelError = new axios.CanceledError('canceled');
      expect(isCancelError(cancelError)).toBe(true);
    });

    it('returns false for regular Error', () => {
      expect(isCancelError(new Error('some error'))).toBe(false);
    });

    it('returns false for non-error values', () => {
      expect(isCancelError(null)).toBe(false);
      expect(isCancelError('string')).toBe(false);
      expect(isCancelError(42)).toBe(false);
    });

    // iter 124: DOMException AbortError branch — withRetry() 在 sleep 中被 abort 时
    // 抛 DOMException('Aborted', 'AbortError'), 旧实现没覆盖会让 abort 错误显示给用户
    // (iter 82 修复)。补测确保 DOMException 路径有断言保护。
    it('returns true for DOMException with AbortError name', () => {
      const domAbort = new DOMException('Aborted', 'AbortError');
      expect(isCancelError(domAbort)).toBe(true);
    });

    it('returns false for DOMException with other names', () => {
      const domOther = new DOMException('InvalidStateError', 'InvalidStateError');
      expect(isCancelError(domOther)).toBe(false);
    });
  });

  describe('withRetry', () => {
    // iter 124: 补测覆盖 0% 的 retry/backoff/abort 路径 — review 报告标注为
    // CRITICAL 可靠性风险, 是核心可靠性特性 (iter 80 引入)。
    it('returns on first success without retrying', async () => {
      const fn = vi.fn().mockResolvedValue('ok');
      const result = await withRetry(fn, { backoffMs: [1, 2, 3] });
      expect(result).toBe('ok');
      expect(fn).toHaveBeenCalledTimes(1);
    });

    it('retries up to N times on transient error then throws lastErr', async () => {
      const lastErr = new Error('boom-final');
      // retries=3 means attempts 0..3 (4 total). After 3rd rejection, attempt=2 < 3,
      // so wait then retry. attempt=3 is the 4th call. fn has only 3 rejections; the
      // 4th call returns undefined (resolved). When we want lastErr to be thrown, we
      // need it on the final attempt. Use 4 rejections so lastErr is at attempt=3.
      const fn = vi
        .fn()
        .mockRejectedValueOnce(new Error('try1'))
        .mockRejectedValueOnce(new Error('try2'))
        .mockRejectedValueOnce(new Error('try3'))
        .mockRejectedValueOnce(lastErr);
      await expect(
        withRetry(fn, { retries: 3, backoffMs: [1, 2, 3] })
      ).rejects.toBe(lastErr);
      expect(fn).toHaveBeenCalledTimes(4);
    });

    it('does not retry on cancel error (axios CanceledError)', async () => {
      const fn = vi.fn().mockRejectedValue(new axios.CanceledError('user-canceled'));
      await expect(withRetry(fn, { backoffMs: [1, 2, 3] })).rejects.toBeInstanceOf(
        axios.CanceledError
      );
      expect(fn).toHaveBeenCalledTimes(1);
    });

    it('throws AbortError immediately if signal already aborted at start', async () => {
      const fn = vi.fn().mockResolvedValue('ok');
      const ctrl = new AbortController();
      ctrl.abort();
      await expect(withRetry(fn, { signal: ctrl.signal })).rejects.toMatchObject({
        name: 'AbortError',
      });
      expect(fn).not.toHaveBeenCalled();
    });

    it('falls back to last backoffMs value when attempt exceeds array length', async () => {
      // 4 次尝试, backoffMs 只有 2 个值 → 第 3 次重试 (attempt=2) 应该用 backoffMs[2] ?? backoffMs[1]
      const fn = vi
        .fn()
        .mockRejectedValueOnce(new Error('try1'))
        .mockRejectedValueOnce(new Error('try2'))
        .mockRejectedValueOnce(new Error('try3'))
        .mockRejectedValueOnce(new Error('try4'));
      // retries=3 default → 4 total attempts (attempt 0,1,2,3)
      // backoffMs[0]=1, backoffMs[1]=2 → attempt=2 should use ?? 2 (last value)
      await expect(
        withRetry(fn, { retries: 3, backoffMs: [1, 2] })
      ).rejects.toThrow('try4');
      expect(fn).toHaveBeenCalledTimes(4);
    });
  });

  describe('analyzePeakTrough', () => {
    // iter 124: 补测 — review 报告标注为 HIGH, 整个函数 0% 覆盖。
    it('POSTs to /analysis/peak-trough with correct body', async () => {
      mockInstance().post.mockResolvedValue({
        data: {
          stock_code: '600519',
          stock_name: '贵州茅台',
          period: 'daily',
          year_high: { date: '2026-01-01', price: 100 },
          year_start_date: '2026-01-01',
          analysis_date: '2026-08-04',
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
          effective_window: 4,
        },
      });

      const result = await analyzePeakTrough('600519', 'daily', 365);

      expect(mockInstance().post).toHaveBeenCalledWith(
        '/analysis/peak-trough',
        { stock_code: '600519', period: 'daily', days: 365 },
        expect.objectContaining({ signal: undefined })
      );
      expect(result.effective_window).toBe(4);
    });

    it('passes AbortSignal through to axios.post', async () => {
      mockInstance().post.mockResolvedValue({ data: {} });
      const ctrl = new AbortController();
      await analyzePeakTrough('AAPL', 'weekly', 90, ctrl.signal);
      const opts = mockInstance().post.mock.calls[0][2] as { signal?: AbortSignal };
      expect(opts.signal).toBe(ctrl.signal);
    });
  });
});
