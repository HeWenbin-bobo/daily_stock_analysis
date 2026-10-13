/** 自选股管理 — localStorage 持久化。 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { StockSearchItem, Market } from '../types';

const STORAGE_KEY = 'watchlist';

interface WatchlistState {
  items: StockSearchItem[];
}

function loadFromStorage(): StockSearchItem[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (x): x is StockSearchItem =>
        x &&
        typeof x.code === 'string' &&
        typeof x.name === 'string' &&
        (x.market === 'cn' || x.market === 'hk' || x.market === 'us')
    );
  } catch {
    return [];
  }
}

function saveToStorage(items: StockSearchItem[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(items));
  } catch {
    // quota exceeded or private mode — ignore
  }
}

/**
 * 自选股管理 Hook — localStorage 持久化。
 *
 * 数据结构: items 为 StockSearchItem[]，每项包含 code/name/market。
 * localStorage 键为 'watchlist'，加载失败时静默返回空数组。
 *
 * @returns items - 自选股列表
 * @returns add(item) - 添加自选股，若已存在 (code+market 相同) 则 no-op
 * @returns remove(code, market) - 移除指定自选股
 * @returns has(code, market) - O(1) 存在性检查，通过 Set-backed useMemo 实现 (iter 94)
 *                              避免 O(n) some() 扫描，且引用稳定
 * @returns clear() - 清空所有自选股
 *
 * localStorage 配额错误处理: saveToStorage 静默 catch，quota exceeded 或隐私模式下不崩溃
 * (iter 125)。
 */
export function useWatchlist(): WatchlistState & {
  add: (item: StockSearchItem) => void;
  // iter 110: market 参数收紧到 Market 联合类型 — 之前 (code: string, market: string)
  // 接受任意字符串, 调用方传 "invalid" 也能编译通过。现在 IDE / tsc 会在传错时报错,
  // 与 watchlistIO.ts 的 VALID_MARKETS 和后端 MarketType Literal 对齐。
  remove: (code: string, market: Market) => void;
  has: (code: string, market: Market) => boolean;
  clear: () => void;
} {
  const [items, setItems] = useState<StockSearchItem[]>(() => loadFromStorage());

  useEffect(() => {
    saveToStorage(items);
  }, [items]);

  // iter 69: 所有 setter / has 用 useCallback 稳定引用 — 避免父组件因 watchlist 整体返回新对象而重渲染
  const add = useCallback((item: StockSearchItem) => {
    setItems((prev) => {
      if (prev.some((p) => p.code === item.code && p.market === item.market)) return prev;
      return [...prev, item];
    });
  }, []);
  const remove = useCallback((code: string, market: Market) => {
    setItems((prev) => prev.filter((p) => !(p.code === code && p.market === market)));
  }, []);
  // iter 94: Set-backed useMemo — 之前 useCallback 每次 items 变都新建 has 函数引用,
  // 且 O(n) some() 扫描。Set 让 has() O(1), hasKey 引用只在 items 增删时变化。
  const hasKey = useMemo(() => {
    const set = new Set<string>();
    for (const item of items) set.add(`${item.market}:${item.code}`);
    return set;
  }, [items]);
  const has = useCallback(
    (code: string, market: Market) => hasKey.has(`${market}:${code}`),
    [hasKey]
  );
  const clear = useCallback(() => setItems([]), []);

  return { items, add, remove, has, clear };
}
