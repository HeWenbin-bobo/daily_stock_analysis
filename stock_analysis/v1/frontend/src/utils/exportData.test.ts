/// <reference types="vitest/globals" />
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { exportAnalysisAsJSON, exportHistoryAsCSV } from '../utils/exportData';
import type { KLineData, PeakTroughResponse } from '../types';

describe('exportData', () => {
  const sampleBars: KLineData[] = [
    { date: '2026-01-01', open: 100, high: 105, low: 99, close: 104, volume: 1000000, amount: 0, change_percent: 0 },
    { date: '2026-01-02', open: 104, high: 108, low: 103, close: 107, volume: 1200000, amount: 0, change_percent: 2.88 },
    { date: '2026-01-03', open: 107, high: 110, low: 106, close: 108, volume: 900000, amount: 0, change_percent: 0.93 },
  ];

  const sampleAnalysis: PeakTroughResponse = {
    stock_code: '600519',
    stock_name: '贵州茅台',
    period: 'daily',
    year_high: { date: '2026-01-02', price: 108 },
    year_start_date: '2026-01-01',
    analysis_date: '2026-01-03',
    segments: [
      {
        type: 'decline',
        from_date: '2026-01-02',
        to_date: '2026-01-03',
        start_price: 108,
        end_price: 108,
        change_pct: 0,
        from_peak: true,
      },
    ],
    summary: {
      max_decline_pct: 0,
      max_rebound_pct: 0,
      current_vs_high_pct: 0,
      current_vs_year_start_pct: 8,
      current_price: 108,
      year_start_price: 100,
      segment_count: 1,
      decline_count: 1,
      rebound_count: 0,
    },
  };

  // Mock setup: 拦截 createElement, URL.createObjectURL, click
  let capturedBlobs: Blob[] = [];
  let capturedAnchors: { download: string; click: () => void }[] = [];

  beforeEach(() => {
    capturedBlobs = [];
    capturedAnchors = [];

    // Stub URL methods
    global.URL.createObjectURL = vi.fn((blob: Blob) => {
      capturedBlobs.push(blob);
      return 'blob:mock-url';
    });
    global.URL.revokeObjectURL = vi.fn();

    // Intercept createElement('a') to capture filename and prevent actual click
    const originalCreateElement = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tag: string) => {
      if (tag === 'a') {
        const anchorProxy = {
          get href() {
            return '';
          },
          set href(_v: string) {
            /* noop */
          },
          get download() {
            return '';
          },
          set download(v: string) {
            anchorProxy._download = v;
          },
          _download: '',
          click: () => {
            capturedAnchors.push({
              download: anchorProxy._download,
              click: () => {},
            });
          },
        };
        return anchorProxy as unknown as HTMLAnchorElement;
      }
      return originalCreateElement(tag);
    });

    // 拦截 appendChild 让其不报错(jsdom 拒绝 mock 的非 DOM 节点)
    const originalAppend = document.body.appendChild.bind(document.body);
    vi.spyOn(document.body, 'appendChild').mockImplementation((node: Node) => {
      // 只接受真正的 DOM Node
      if (node instanceof Node) return originalAppend(node);
      throw new TypeError('not a Node');
    });
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('exportHistoryAsCSV', () => {
    it('produces a CSV Blob', () => {
      exportHistoryAsCSV('600519', '贵州茅台', 'daily', sampleBars);
      expect(capturedBlobs).toHaveLength(1);
      expect(capturedAnchors).toHaveLength(1);
      const blob = capturedBlobs[0];
      expect(blob).toBeInstanceOf(Blob);
      expect(blob.type).toContain('text/csv');
      // 文件名包含 code + period
      expect(capturedAnchors[0].download).toMatch(/600519_daily_\d{8}_\d{4}\.csv/);
    });

    it('includes segments table when provided', async () => {
      exportHistoryAsCSV('600519', '贵州茅台', 'daily', sampleBars, sampleAnalysis.segments);
      const blob = capturedBlobs[0];
      const text = await blob.text();
      expect(text).toContain('# Peak/Trough segments');
      expect(text).toContain('decline');
      expect(text).toContain('2026-01-02');
    });

    it('includes UTF-8 BOM for Excel compatibility', async () => {
      exportHistoryAsCSV('600519', '贵州茅台', 'daily', sampleBars);
      const blob = capturedBlobs[0];
      const buffer = await blob.arrayBuffer();
      const bytes = new Uint8Array(buffer);
      // UTF-8 BOM = 0xEF 0xBB 0xBF
      expect(bytes[0]).toBe(0xef);
      expect(bytes[1]).toBe(0xbb);
      expect(bytes[2]).toBe(0xbf);
      // 后续内容是 CSV 内容
      const text = new TextDecoder('utf-8').decode(bytes);
      expect(text).toContain('date,open,high,low,close,volume,amount,change_percent');
    });

    it('escapes commas, quotes, and newlines in date fields', async () => {
      const tricky: KLineData[] = [
        { date: '2026-01-01,"has,comma"', open: 1, high: 2, low: 0.5, close: 1.5, volume: 100, amount: 0, change_percent: 0 },
        { date: '2026-01-02\nfake-row', open: 1, high: 2, low: 0.5, close: 1.5, volume: 100, amount: 0, change_percent: 0 },
      ];
      exportHistoryAsCSV('X', 'Tricky, Inc.', 'daily', tricky);
      const text = await capturedBlobs[0].text();
      // 含逗号/引号的字段被双引号包裹, 内部引号双写
      expect(text).toContain('"2026-01-01,""has,comma"""');
      // 含换行的字段被双引号包裹, 内部换行被保留(CSV 规范允许 quoted 字段含换行)
      expect(text).toContain('"2026-01-02\nfake-row"');
      // 两个日期都出现在输出中 (不管是否在同一物理行)
      const occurrences = (text.match(/2026-01-0[12]/g) ?? []).length;
      expect(occurrences).toBeGreaterThanOrEqual(2);
    });

    it('prefixes Excel formula characters on text fields to prevent injection', async () => {
      const evil: KLineData[] = [
        { date: '=SUM(A1:A2)', open: 1, high: 1, low: 1, close: 1, volume: 1, amount: 0, change_percent: 0 },
        { date: '+cmd|calc', open: 1, high: 1, low: 1, close: 1, volume: 1, amount: 0, change_percent: 0 },
        { date: '-1+1', open: 1, high: 1, low: 1, close: 1, volume: 1, amount: 0, change_percent: 0 },
        { date: '@evil', open: 1, high: 1, low: 1, close: 1, volume: 1, amount: 0, change_percent: 0 },
      ];
      exportHistoryAsCSV('X', 'Evil', 'daily', evil);
      const text = await capturedBlobs[0].text();
      // 危险前缀被加单引号转义 (作为字段值)
      expect(text).toContain("'=SUM(A1:A2)");
      expect(text).toContain("'+cmd|calc");
      expect(text).toContain("'-1+1");
      expect(text).toContain("'@evil");
    });

    it('escapes segment type/text fields too', async () => {
      exportHistoryAsCSV(
        'X', 'Name', 'daily', sampleBars,
        [{ type: '=BAD()', from_date: '2026-01-01', to_date: '2026-01-02', start_price: 1, end_price: 2, change_pct: 1, from_peak: false }]
      );
      const text = await capturedBlobs[0].text();
      expect(text).toContain("'=BAD()");
    });

    it('includes # Source: header when source provided (iter 157)', async () => {
      exportHistoryAsCSV('AAPL', 'Apple Inc.', 'daily', sampleBars, undefined, 'yfinance');
      const text = await capturedBlobs[0].text();
      expect(text).toContain('# Source: yfinance');
    });

    it('omits # Source: header when source not provided (backward compat)', async () => {
      exportHistoryAsCSV('AAPL', 'Apple Inc.', 'daily', sampleBars);
      const text = await capturedBlobs[0].text();
      expect(text).not.toContain('# Source:');
    });
  });

  describe('exportAnalysisAsJSON', () => {
    it('produces JSON with full payload', async () => {
      exportAnalysisAsJSON(sampleAnalysis, sampleBars);
      const blob = capturedBlobs[0];
      expect(blob.type).toContain('application/json');
      const text = await blob.text();
      const parsed = JSON.parse(text);
      expect(parsed.analysis.stock_code).toBe('600519');
      expect(parsed.history).toHaveLength(3);
      expect(parsed.exported_at).toBeDefined();
    });

    it('filename includes stock code', () => {
      exportAnalysisAsJSON(sampleAnalysis, sampleBars);
      expect(capturedAnchors[0].download).toMatch(/600519_analysis_\d{8}_\d{4}\.json/);
    });

    it('includes source field at top level when provided (iter 157)', async () => {
      exportAnalysisAsJSON(sampleAnalysis, sampleBars, 'mock_fallback');
      const text = await capturedBlobs[0].text();
      const parsed = JSON.parse(text);
      expect(parsed.source).toBe('mock_fallback');
    });

    it('omits source field when not provided (backward compat)', async () => {
      exportAnalysisAsJSON(sampleAnalysis, sampleBars);
      const text = await capturedBlobs[0].text();
      const parsed = JSON.parse(text);
      expect(parsed).not.toHaveProperty('source');
    });
  });
});
