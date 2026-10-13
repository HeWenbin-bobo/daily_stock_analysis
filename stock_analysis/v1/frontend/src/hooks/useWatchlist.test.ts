import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useWatchlist } from './useWatchlist';
import type { StockSearchItem } from '../types';

describe('useWatchlist', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  const sample: StockSearchItem = { code: '600519', name: '贵州茅台', market: 'cn' };

  it('starts with empty list', () => {
    const { result } = renderHook(() => useWatchlist());
    expect(result.current.items).toEqual([]);
  });

  it('adds item', () => {
    const { result } = renderHook(() => useWatchlist());
    act(() => result.current.add(sample));
    expect(result.current.items).toHaveLength(1);
    expect(result.current.items[0]).toEqual(sample);
  });

  it('does not duplicate same code+market', () => {
    const { result } = renderHook(() => useWatchlist());
    act(() => {
      result.current.add(sample);
      result.current.add(sample);
    });
    expect(result.current.items).toHaveLength(1);
  });

  it('removes item', () => {
    const { result } = renderHook(() => useWatchlist());
    act(() => result.current.add(sample));
    act(() => result.current.remove('600519', 'cn'));
    expect(result.current.items).toHaveLength(0);
  });

  it('has() returns true for existing items', () => {
    const { result } = renderHook(() => useWatchlist());
    act(() => result.current.add(sample));
    expect(result.current.has('600519', 'cn')).toBe(true);
    expect(result.current.has('AAPL', 'us')).toBe(false);
  });

  it('clear() empties list', () => {
    const { result } = renderHook(() => useWatchlist());
    act(() => {
      result.current.add(sample);
      result.current.add({ code: 'AAPL', name: 'Apple', market: 'us' });
    });
    expect(result.current.items).toHaveLength(2);
    act(() => result.current.clear());
    expect(result.current.items).toHaveLength(0);
  });

  it('persists to localStorage', () => {
    const { result } = renderHook(() => useWatchlist());
    act(() => result.current.add(sample));
    // localStorage should have the item
    const stored = JSON.parse(localStorage.getItem('watchlist') || '[]');
    expect(stored).toHaveLength(1);
    expect(stored[0].code).toBe('600519');
  });

  it('loads from localStorage on mount', () => {
    localStorage.setItem(
      'watchlist',
      JSON.stringify([{ code: 'AAPL', name: 'Apple', market: 'us' }])
    );
    const { result } = renderHook(() => useWatchlist());
    expect(result.current.items).toHaveLength(1);
    expect(result.current.items[0].code).toBe('AAPL');
  });

  it('filters out invalid stored items', () => {
    localStorage.setItem(
      'watchlist',
      JSON.stringify([
        { code: 'GOOD', name: 'Good', market: 'cn' },
        { code: 'BAD' },  // missing fields
        null,
        'string',
      ])
    );
    const { result } = renderHook(() => useWatchlist());
    expect(result.current.items).toHaveLength(1);
    expect(result.current.items[0].code).toBe('GOOD');
  });

  it('handles corrupted localStorage gracefully', () => {
    localStorage.setItem('watchlist', 'not-json{{{}');
    const { result } = renderHook(() => useWatchlist());
    expect(result.current.items).toEqual([]);
  });

  // iter 125: 补测 QuotaExceededError 静默吞错 — review 报告标注为 MEDIUM。
  // useWatchlist.ts 的 saveToStorage 用 try/except Exception 把 localStorage.setItem
  // 失败吞掉。如果这个 catch 漏了, 内存状态会回退, 用户看到 UI 与 localStorage 不一致。
  it('silently catches QuotaExceededError when localStorage is full', () => {
    const setItemSpy = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Quota exceeded', 'QuotaExceededError');
    });
    const { result } = renderHook(() => useWatchlist());
    // 关键断言: add() 不应抛出 — catch block 把它吞掉
    expect(() => {
      act(() => result.current.add(sample));
    }).not.toThrow();
    setItemSpy.mockRestore();
  });
});
