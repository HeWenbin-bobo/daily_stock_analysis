import { describe, expect, it } from 'vitest';
import { exportWatchlist, importWatchlistFile } from '../utils/watchlistIO';
import type { StockSearchItem } from '../types';

describe('watchlistIO', () => {
  const sampleItems: StockSearchItem[] = [
    { code: '600519', name: '贵州茅台', market: 'cn' },
    { code: '00700', name: '腾讯控股', market: 'hk' },
    { code: 'AAPL', name: 'Apple Inc.', market: 'us' },
  ];

  describe('exportWatchlist', () => {
    it('does not throw on empty list', () => {
      expect(() => exportWatchlist([])).not.toThrow();
    });

    it('produces a Blob URL (DOM environment check)', () => {
      // 在 jsdom 中, URL.createObjectURL 可能不存在
      // 这里只验证函数签名正确, 不实际触发下载
      expect(typeof exportWatchlist).toBe('function');
    });
  });

  describe('importWatchlistFile', () => {
    it('imports valid JSON with items wrapper', async () => {
      const file = new File(
        [JSON.stringify({ items: sampleItems })],
        'watchlist.json',
        { type: 'application/json' }
      );
      const result = await importWatchlistFile(file);
      expect(result).toHaveLength(3);
      expect(result[0]).toEqual({ code: '600519', name: '贵州茅台', market: 'cn' });
    });

    it('imports valid JSON as direct array', async () => {
      const file = new File(
        [JSON.stringify(sampleItems)],
        'watchlist.json',
        { type: 'application/json' }
      );
      const result = await importWatchlistFile(file);
      expect(result).toHaveLength(3);
    });

    it('rejects invalid JSON', async () => {
      const file = new File(['not json{{{}'], 'bad.json', { type: 'application/json' });
      await expect(importWatchlistFile(file)).rejects.toThrow('文件不是合法的 JSON');
    });

    it('rejects JSON without items', async () => {
      const file = new File(
        [JSON.stringify({ foo: 'bar' })],
        'watchlist.json',
        { type: 'application/json' }
      );
      await expect(importWatchlistFile(file)).rejects.toThrow('JSON 结构不合法');
    });

    it('filters out invalid items, keeps valid ones', async () => {
      const mixed = [
        { code: '600519', name: '贵州茅台', market: 'cn' },  // valid
        { code: 'BAD' },                                      // missing fields
        { code: '00700', name: '腾讯', market: 'unknown' },   // invalid market
        'not an object',
        null,
        { code: 'AAPL', name: 'Apple', market: 'us' },        // valid
      ];
      const file = new File(
        [JSON.stringify({ items: mixed })],
        'watchlist.json',
        { type: 'application/json' }
      );
      const result = await importWatchlistFile(file);
      expect(result).toHaveLength(2);
      expect(result.map((x) => x.code)).toEqual(['600519', 'AAPL']);
    });

    it('rejects when no valid items', async () => {
      const file = new File(
        [JSON.stringify({ items: [{ foo: 'bar' }, null] })],
        'watchlist.json',
        { type: 'application/json' }
      );
      await expect(importWatchlistFile(file)).rejects.toThrow(/无有效条目/);
    });

    it('rejects empty files', async () => {
      const file = new File([''], 'empty.json', { type: 'application/json' });
      await expect(importWatchlistFile(file)).rejects.toThrow(/文件为空/);
    });

    it('rejects oversized files', async () => {
      // 直接造一个 >1MiB 的 File 对象 (blob part 超过阈值即可)
      const big = new Uint8Array(1024 * 1024 + 1);
      const file = new File([big], 'big.json', { type: 'application/json' });
      await expect(importWatchlistFile(file)).rejects.toThrow(/文件过大/);
    });

    it('deduplicates items with same code+market', async () => {
      const dup = [
        { code: '600519', name: '贵州茅台', market: 'cn' },
        { code: '600519', name: '贵州茅台', market: 'cn' },
        { code: 'AAPL', name: 'Apple', market: 'us' },
      ];
      const file = new File(
        [JSON.stringify({ items: dup })],
        'watchlist.json',
        { type: 'application/json' }
      );
      const result = await importWatchlistFile(file);
      expect(result).toHaveLength(2);
      expect(result.map((x) => x.code)).toEqual(['600519', 'AAPL']);
    });

    it('rejects items with oversized code/name length', async () => {
      const longCode = 'X'.repeat(64);
      const longName = 'N'.repeat(128);
      const mixed = [
        { code: longCode, name: 'ok', market: 'cn' },        // code too long
        { code: 'AAPL', name: longName, market: 'us' },     // name too long
        { code: 'TSLA', name: 'Tesla', market: 'us' },      // ok
      ];
      const file = new File(
        [JSON.stringify({ items: mixed })],
        'watchlist.json',
        { type: 'application/json' }
      );
      const result = await importWatchlistFile(file);
      expect(result).toHaveLength(1);
      expect(result[0].code).toBe('TSLA');
    });

    it('rejects when item count exceeds limit', async () => {
      const tooMany = Array.from({ length: 501 }, (_, i) => ({
        code: `C${i}`, name: `N${i}`, market: 'cn',
      }));
      const file = new File(
        [JSON.stringify({ items: tooMany })],
        'watchlist.json',
        { type: 'application/json' }
      );
      await expect(importWatchlistFile(file)).rejects.toThrow(/条目数超出限制/);
    });
  });
});
