/// <reference types="vitest/globals" />
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, cleanup, fireEvent, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { PeakTroughPanel } from './PeakTroughPanel';
import type { PeakTroughResponse } from '../types';

// Mock 整个 api/client 模块
const mockFetchLLMStatus = vi.fn();
const mockFetchLLMInsight = vi.fn();

vi.mock('../api/client', () => ({
  fetchLLMStatus: () => mockFetchLLMStatus(),
  fetchLLMInsight: (...args: unknown[]) => mockFetchLLMInsight(...args),
  isCancelError: (err: unknown) =>
    err instanceof Error && /abort|cancel/i.test(err.message),
}));

const sampleAnalysis: PeakTroughResponse = {
  stock_code: '600519',
  stock_name: '贵州茅台',
  period: 'daily',
  year_high: { date: '2026-04-22', price: 64.40 },
  year_start_date: '2026-01-01',
  analysis_date: '2026-08-02',
  segments: [
    {
      type: 'decline',
      from_date: '2026-04-22',
      to_date: '2026-05-01',
      start_price: 64.40,
      end_price: 55.78,
      change_pct: -13.39,
      from_peak: true,
    },
    {
      type: 'rebound',
      from_date: '2026-05-01',
      to_date: '2026-05-06',
      start_price: 55.78,
      end_price: 59.58,
      change_pct: 6.83,
      from_peak: false,
    },
    {
      type: 'decline',
      from_date: '2026-05-06',
      to_date: '2026-05-16',
      start_price: 59.58,
      end_price: 48.20,
      change_pct: -19.11,
      from_peak: false,
    },
  ],
  summary: {
    max_decline_pct: -23.32,
    max_rebound_pct: 11.62,
    current_vs_high_pct: -53.08,
    current_vs_year_start_pct: -47.23,
    current_price: 30.22,
    year_start_price: 57.26,
    segment_count: 3,
    decline_count: 2,
    rebound_count: 1,
  },
};

describe('PeakTroughPanel', () => {
  beforeEach(() => {
    mockFetchLLMStatus.mockResolvedValue({
      available: true,
      source: 'cc_switch',
      provider: 'MiniMax',
      model: 'MiniMax-M3',
      base_url: 'https://api.minimaxi.com',
      app_type: 'claude',
      all_providers: [],
    });
    mockFetchLLMInsight.mockResolvedValue({
      provider: 'MiniMax',
      model: 'MiniMax-M3',
      text: '技术分析: 该股处于下跌趋势',
      source: 'cc_switch',
    });
  });

  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
    // 重置 mock 实现 (mockRejectedValueOnce / mockResolvedValueOnce 的实现状态),
    // 避免跨测试串扰导致泄漏
    mockFetchLLMStatus.mockReset();
    mockFetchLLMInsight.mockReset();
  });

  describe('top summary', () => {
    it('renders year high with date', () => {
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      expect(screen.getByText(/年度最高点/)).toBeInTheDocument();
      // 64.40 出现在多个地方(年份+segment),用 getAllByText
      expect(screen.getAllByText(/64\.40/).length).toBeGreaterThan(0);
      // 2026-04-22 also appears multiple times
      expect(screen.getAllByText(/2026-04-22/).length).toBeGreaterThan(0);
    });

    it('renders current price', () => {
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      expect(screen.getByText(/当前价/)).toBeInTheDocument();
      expect(screen.getByText(/30\.22/)).toBeInTheDocument();
    });

    it('renders year start price', () => {
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      expect(screen.getByText(/年初价/)).toBeInTheDocument();
      expect(screen.getByText(/57\.26/)).toBeInTheDocument();
    });

    it('renders year-to-date return', () => {
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      expect(screen.getByText(/年初至今/)).toBeInTheDocument();
      expect(screen.getByText(/-47\.23%/)).toBeInTheDocument();
    });

    it('renders distance from year high', () => {
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      expect(screen.getByText(/距年度高点/)).toBeInTheDocument();
      expect(screen.getByText(/-53\.08%/)).toBeInTheDocument();
    });
  });

  describe('segments table', () => {
    it('renders all segments', () => {
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      expect(screen.getByText(/回撤.*自高点/)).toBeInTheDocument();
      expect(screen.getByText(/回弹/)).toBeInTheDocument();
    });

    it('shows segment dates', () => {
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      // 多重日期显示
      expect(screen.getAllByText(/2026-04-22/).length).toBeGreaterThan(0);
      expect(screen.getAllByText(/2026-05-01/).length).toBeGreaterThan(0);
    });

    it('shows segment prices', () => {
      const { container } = render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      // 64.40 出现在多个地方(年份+segment),用 container 验证
      expect(container.textContent).toContain('64.40');
      expect(container.textContent).toContain('55.78');
    });

    it('shows segment change percentages with proper sign', () => {
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      expect(screen.getByText('-13.39%')).toBeInTheDocument();
      expect(screen.getByText('+6.83%')).toBeInTheDocument();
    });
  });

  describe('AI insight', () => {
    it('shows AI button with provider name', async () => {
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      await waitFor(() => {
        expect(screen.getByText(/当前 LLM: MiniMax/)).toBeInTheDocument();
      });
    });

    it('clicking AI button shows analysis', async () => {
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      const aiButton = await screen.findByText(/AI 解读/);
      fireEvent.click(aiButton);

      await waitFor(() => {
        expect(screen.getByText(/技术分析: 该股处于下跌趋势/)).toBeInTheDocument();
      });
      // 验证调用 - 第三个参数 days 是 365 (默认)
      const calls = mockFetchLLMInsight.mock.calls;
      expect(calls.length).toBeGreaterThan(0);
      expect(calls[0][0]).toBe('600519');
      expect(calls[0][1]).toBe('daily');
    });

    it('passes custom days and AbortSignal to fetchLLMInsight', async () => {
      render(
        <PeakTroughPanel
          analysis={sampleAnalysis}
          stockCode="600519"
          period="daily"
          days={120}
        />
      );
      const aiButton = await screen.findByText(/AI 解读/);
      fireEvent.click(aiButton);

      await waitFor(() => {
        expect(mockFetchLLMInsight).toHaveBeenCalled();
      });
      const calls = mockFetchLLMInsight.mock.calls;
      // (code, period, days, extraPrompt?, signal?)
      expect(calls[0][0]).toBe('600519');
      expect(calls[0][1]).toBe('daily');
      expect(calls[0][2]).toBe(120);
      // signal 是第 5 个参数(extraPrompt? 之后)
      const signal = calls[0][calls[0].length - 1];
      expect(signal).toBeDefined();
      expect(typeof (signal as { aborted?: boolean }).aborted).toBe('boolean');
    });

    it('clears AI output when switching stock (does not show stale text)', async () => {
      mockFetchLLMInsight.mockResolvedValueOnce({
        provider: 'MiniMax',
        model: 'MiniMax-M3',
        text: 'OLD_RESPONSE_TEXT_AAA',
        source: 'cc_switch',
      });
      const { rerender } = render(
        <PeakTroughPanel
          analysis={sampleAnalysis}
          stockCode="600519"
          period="daily"
        />
      );
      const aiButton = await screen.findByText(/AI 解读/);
      fireEvent.click(aiButton);
      await waitFor(() => {
        expect(screen.getByText(/OLD_RESPONSE_TEXT_AAA/)).toBeInTheDocument();
      });
      // 切换股票: 应清空旧 AI 文本
      rerender(
        <PeakTroughPanel
          analysis={{ ...sampleAnalysis, stock_code: '000001' }}
          stockCode="000001"
          period="daily"
        />
      );
      expect(screen.queryByText(/OLD_RESPONSE_TEXT_AAA/)).not.toBeInTheDocument();
    });

    it('ignores stale response after stockCode change (generation guard)', async () => {
      // Stale 的第一次响应 (我们会延迟 resolve 来模拟"晚到的旧请求")
      let resolveStale!: (v: unknown) => void;
      mockFetchLLMInsight
        .mockImplementationOnce(
          () =>
            new Promise((resolve) => {
              resolveStale = resolve;
            })
        )
        // 第二次 (切换 stock 之后用户再次点击) 拿到的响应
        .mockResolvedValueOnce({
          provider: 'MiniMax',
          model: 'MiniMax-M3',
          text: 'NEW_RESPONSE_TEXT_BBB',
          source: 'cc_switch',
        });
      const { rerender } = render(
        <PeakTroughPanel
          analysis={sampleAnalysis}
          stockCode="600519"
          period="daily"
        />
      );
      const aiButton = await screen.findByText(/AI 解读/);
      fireEvent.click(aiButton);

      // 立即切换 stock 触发 useEffect: abort + increment token + clear ai
      // 此时旧 promise 仍 pending, 但已被 generation guard 标记为过期
      rerender(
        <PeakTroughPanel
          analysis={{ ...sampleAnalysis, stock_code: '000001' }}
          stockCode="000001"
          period="daily"
        />
      );

      // 第二次点击 - 此时按钮不再 disabled (useEffect 重置了 aiLoading), 正常 fireEvent
      const aiButton2 = await screen.findByText(/AI 解读/);
      fireEvent.click(aiButton2);

      // 等待新的响应被渲染
      await waitFor(() => {
        expect(screen.getByText(/NEW_RESPONSE_TEXT_BBB/)).toBeInTheDocument();
      });

      // 现在才释放 stale 响应 - 应被 generation guard 丢弃, 不覆盖新文本
      resolveStale({
        provider: 'OLD',
        model: 'OLD',
        text: 'STALE_RESPONSE_SHOULD_NOT_SHOW',
        source: 'old',
      });
      // 给 microtask 时间
      await new Promise((r) => setTimeout(r, 30));
      expect(screen.queryByText(/STALE_RESPONSE_SHOULD_NOT_SHOW/)).not.toBeInTheDocument();
    });

    it('shows safe generic message for raw technical error', async () => {
      const longErr =
        'Request failed with status code 500 [stack trace at /api/v1/analysis/llm-insight with various technical details and headers that should not be shown to user]';
      mockFetchLLMInsight.mockRejectedValueOnce(new Error(longErr));
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      const aiButton = await screen.findByText(/AI 解读/);
      fireEvent.click(aiButton);

      await waitFor(() => {
        // 非 AxiosError 一律降级到通用提示, 绝不暴露 raw message
        expect(screen.getByText(/请求失败, 请稍后重试/)).toBeInTheDocument();
        const alert = screen.getByRole('alert');
        expect(alert.textContent).not.toContain('stack trace');
        expect(alert.textContent).not.toContain('headers that should not be shown');
      });
    });

    it('shows safe network message when network fails', async () => {
      mockFetchLLMInsight.mockRejectedValueOnce(new Error('Network Error: ECONNRESET'));
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      const aiButton = await screen.findByText(/AI 解读/);
      fireEvent.click(aiButton);

      await waitFor(() => {
        // 非 AxiosError → 通用 fallback, 不暴露 raw message
        expect(screen.getByText(/请求失败, 请稍后重试/)).toBeInTheDocument();
        const alert = screen.getByRole('alert');
        expect(alert.textContent).not.toMatch(/Network Error: ECONNRESET/);
      });
    });

    it('AI button disabled when LLM not available', async () => {
      mockFetchLLMStatus.mockResolvedValue({
        available: false,
        source: 'none',
        provider: null,
        model: null,
        base_url: null,
        app_type: null,
        all_providers: [],
      });
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      await waitFor(() => {
        const aiButton = screen.getByText(/AI 解读/).closest('button')!;
        expect(aiButton).toBeDisabled();
      });
    });

    it('shows error message when LLM call fails', async () => {
      mockFetchLLMInsight.mockRejectedValue(new Error('LLM error'));
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      const aiButton = await screen.findByText(/AI 解读/);
      fireEvent.click(aiButton);

      await waitFor(() => {
        // 非 AxiosError → 通用 fallback, 不暴露 raw "LLM error"
        expect(screen.getByText(/请求失败, 请稍后重试/)).toBeInTheDocument();
        const alert = screen.getByRole('alert');
        expect(alert.textContent).not.toContain('LLM error');
      });
      // 检查重试按钮 (aria-label 现在是 "重试 AI 解读", 优先于可见文本)
      expect(screen.getByRole('button', { name: '重试 AI 解读' })).toBeInTheDocument();
    });

    it('displays provider name after AI call', async () => {
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      const aiButton = await screen.findByText(/AI 解读/);
      fireEvent.click(aiButton);

      await waitFor(() => {
        expect(screen.getByText(/— MiniMax \/ MiniMax-M3/)).toBeInTheDocument();
      });
    });

    it('null/undefined AI text does not crash and renders nothing', async () => {
      // iter 63 防御: 后端可能在极端情况下返回 {provider, model, text: undefined} 或 text: null
      mockFetchLLMInsight.mockResolvedValueOnce({
        provider: 'MiniMax',
        model: 'MiniMax-M3',
        text: undefined as unknown as string, // 故意类型作弊模拟
        source: 'cc_switch',
      });
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      const aiButton = await screen.findByText(/AI 解读/);
      fireEvent.click(aiButton);

      // 等 loading 完成, 没有 error alert
      await waitFor(() => {
        // 按钮应该恢复为非 disabled + 非 loading 状态
        const btn = screen.getByText(/AI 解读/).closest('button')!;
        expect(btn).not.toBeDisabled();
      });
      // 没有 error alert
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
      // 验证 aiHtml 区域没渲染(因为 text 是空串 → aiText 为空 → {aiText && ...} 不渲染)
      // 但 aiMeta 是空, 所以 provider 行也不渲染; 关键是"按钮恢复 + 无 error"就够
    });

    it('non-string AI text (e.g. number) is treated as empty', async () => {
      mockFetchLLMInsight.mockResolvedValueOnce({
        provider: 'MiniMax',
        model: 'MiniMax-M3',
        text: 12345 as unknown as string,
        source: 'cc_switch',
      });
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      const aiButton = await screen.findByText(/AI 解读/);
      fireEvent.click(aiButton);

      // 不崩, 按钮恢复
      await waitFor(() => {
        const btn = screen.getByText(/AI 解读/).closest('button')!;
        expect(btn).not.toBeDisabled();
      });
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });
  });

  describe('export buttons', () => {
    it('shows CSV export button when history provided', () => {
      const sampleBars = [
        { date: '2026-01-01', open: 100, high: 105, low: 99, close: 104, volume: 1e6, amount: 0, change_percent: 0 },
      ];
      render(
        <PeakTroughPanel
          analysis={sampleAnalysis}
          stockCode="600519"
          period="daily"
          history={sampleBars}
        />
      );
      // iter 100: aria-label 改为动态 `${name} (${code}) X 为 Y 文件` — 用 substring 匹配
      // 让测试对 name/code 顺序不敏感。
      expect(screen.getByLabelText(/导出.*K线数据为 CSV 文件/)).toBeInTheDocument();
      expect(screen.getByLabelText(/导出.*完整分析结果为 JSON 文件/)).toBeInTheDocument();
    });

    it('does not show export buttons when history missing', () => {
      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      expect(screen.queryByLabelText('导出 CSV')).not.toBeInTheDocument();
    });
  });

  describe('AI error accessibility', () => {
    it('renders AI error alert with role=alert and aria-live', async () => {
      mockFetchLLMStatus.mockResolvedValue({
        available: true,
        provider: 'MiniMax',
        model: 'MiniMax-M3',
        source: 'cc_switch',
        all_providers: 1,
      });
      mockFetchLLMInsight.mockRejectedValue(new Error('boom'));

      render(<PeakTroughPanel analysis={sampleAnalysis} stockCode="600519" period="daily" />);
      // 等 LLM 状态 useEffect 跑完, 按钮 enabled 后才能点击
      const button = await screen.findByRole('button', { name: /✨ AI 解读/ });
      fireEvent.click(button);

      const alert = await screen.findByRole('alert');
      expect(alert).toBeInTheDocument();
      expect(alert.getAttribute('aria-live')).toBe('assertive');
      expect(screen.getByLabelText('重试 AI 解读')).toBeInTheDocument();
    });
  });
});
