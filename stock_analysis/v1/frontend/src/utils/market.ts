import type { Market } from '../types';

/** 股票市场显示名 (iter 66: 从 SearchBar/Watchlist 提取, 避免双定义漂移) */
export function marketLabel(m: Market): string {
  return m === 'cn' ? 'A股' : m === 'hk' ? '港股' : '美股';
}

/**
 * 股票市场对应 Tailwind 背景色 (用于 badge)。
 *
 * @param m - 市场标识
 * @returns A股 → 'bg-red-600'，港股 → 'bg-yellow-600'，美股 → 'bg-blue-600'
 */
export function marketColor(m: Market): string {
  return m === 'cn' ? 'bg-red-600' : m === 'hk' ? 'bg-yellow-600' : 'bg-blue-600';
}