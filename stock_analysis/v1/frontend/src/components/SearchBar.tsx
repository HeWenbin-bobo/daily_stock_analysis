/**
 * 股票搜索下拉组合框, 300ms 防抖减少 API 请求频率 (iter 74)。
 * 支持方向键导航、Enter 选中、Escape 关闭, listbox 模式使用
 * aria-activedescendant 维护可访问性 (iter 69, 119)。
 */
import { memo, useCallback, useEffect, useRef, useState } from 'react';
import { isCancelError, searchStocks } from '../api/client';
import { toUserMessage } from '../utils/errors';
import { marketColor, marketLabel } from '../utils/market';
import type { StockSearchItem } from '../types';

const SEARCH_DEBOUNCE_MS = 300;

interface Props {
  onSelect: (s: StockSearchItem) => void;
}

// iter 107: 加 React.memo — 与其它交互组件 (Watchlist/KLineChart/PeriodSelector 等) 对齐,
// 避免父组件 (App) 因股票切换 / theme toggle 等重新渲染时, SearchBar 也白白跑一遍 useEffect。
// 内部所有 handler 已 useCallback 稳定, props 只有 onSelect, memo 即可生效。
export const SearchBar = memo(function SearchBar({ onSelect }: Props) {
  const [q, setQ] = useState('');
  const [items, setItems] = useState<StockSearchItem[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string>('');
  const [activeIndex, setActiveIndex] = useState<number>(-1);
  const containerRef = useRef<HTMLDivElement>(null);
  // 记录刚被选中条目的展示文本, 用于跳过其触发的 re-search,
  // 避免 "Apple Inc. (AAPL)" 被搜为 "未找到匹配"。
  const lastSelectedLabelRef = useRef<string>('');
  const listboxId = 'searchbar-listbox';
  const inputId = 'searchbar-input';

  // 修复 HIGH race condition: 用 AbortController 取消旧请求, 避免乱序覆盖
  useEffect(() => {
    if (!q.trim()) {
      // 清空输入: 立即重置 loading + 关闭面板, 避免搜索中残留 spinner
      setItems([]);
      setError('');
      setLoading(false);
      setOpen(false);
      setActiveIndex(-1);
      return;
    }
    // B-3 修复: 选中条目后回填的展示文本无需触发搜索
    if (q === lastSelectedLabelRef.current) {
      lastSelectedLabelRef.current = '';
      setItems([]);
      setError('');
      setLoading(false);
      setOpen(false);
      setActiveIndex(-1);
      return;
    }
    const ctrl = new AbortController();
    // iter 74: setLoading(true) 移到 setTimeout 内部 — 用户输入时不再立即看到
    // "搜索中...", 300ms 防抖结束后才显示, 减少视觉抖动
    setError('');
    setActiveIndex(-1);
    const handle = setTimeout(async () => {
      setLoading(true);
      try {
        const results = await searchStocks(q, ctrl.signal);
        if (!ctrl.signal.aborted) {
          setItems(results);
          setOpen(true);
        }
      } catch (err: unknown) {
        if (isCancelError(err) || ctrl.signal.aborted) return;
        if (import.meta.env.DEV) {
          console.error('搜索失败:', err);
        }
        if (!ctrl.signal.aborted) {
          setError(toUserMessage(err));
          setItems([]);
        }
      } finally {
        if (!ctrl.signal.aborted) setLoading(false);
      }
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      clearTimeout(handle);
      ctrl.abort();
    };
  }, [q]);

  // 点击外部关闭
  // iter 69: 用 useCallback 稳定 handler 引用, 避免 effect 每 render 重注册
  const handleClickOutside = useCallback((e: MouseEvent) => {
    if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
      setOpen(false);
    }
  }, []);
  useEffect(() => {
    // iter 81: passive: true — 让浏览器承诺不调用 preventDefault, 让 mousedown 路径
    // 走 fast path, 避免移动端下拉打开时滚动卡顿
    document.addEventListener('mousedown', handleClickOutside, { passive: true });
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [handleClickOutside]);

  // 基础键盘操作: ArrowDown/ArrowUp/Enter/Escape
  // iter 69: 用 useCallback 稳定 keydown handler 引用 (input 是受控组件, 每次 keystroke 都 render)
  // iter 119: scrollIntoView — 之前 setActiveIndex 后没有把对应 li 滚入视口,
  // 用户 arrow-down 到 listbox 底部之外的项目时, active 是"看不见的",
  // 不知道当前高亮的是哪一项 (尤其长列表)。与 useRovingTabIndex 一致 (block: nearest)。
  const scrollOptionIntoView = useCallback((index: number) => {
    const el = document.getElementById(`searchbar-option-${index}`);
    el?.scrollIntoView({ block: "nearest" });
  }, []);

  const handleInputKeyDown = useCallback((e: React.KeyboardEvent<HTMLInputElement>) => {
    if (!open || items.length === 0) return;
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveIndex((i) => {
        const next = (i + 1) % items.length;
        scrollOptionIntoView(next);
        return next;
      });
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveIndex((i) => {
        const next = i <= 0 ? items.length - 1 : i - 1;
        scrollOptionIntoView(next);
        return next;
      });
    } else if (e.key === 'Enter') {
      if (activeIndex >= 0 && activeIndex < items.length) {
        const it = items[activeIndex];
        if (!it) return; // iter 71: noUncheckedIndexedAccess 兜底
        e.preventDefault();
        onSelect(it);
        setOpen(false);
        const label = `${it.name} (${it.code})`;
        lastSelectedLabelRef.current = label;
        setQ(label);
        setActiveIndex(-1);
      }
    } else if (e.key === 'Tab') {
      if (activeIndex >= 0 && activeIndex < items.length) {
        const it = items[activeIndex];
        if (!it) return; // iter 71: noUncheckedIndexedAccess 兜底
        e.preventDefault();
        onSelect(it);
        setOpen(false);
        const label = `${it.name} (${it.code})`;
        lastSelectedLabelRef.current = label;
        setQ(label);
        setActiveIndex(-1);
      }
    } else if (e.key === 'Escape') {
      setOpen(false);
      setActiveIndex(-1);
    }
  }, [open, items, activeIndex, onSelect, scrollOptionIntoView]);

  return (
    <div ref={containerRef} className="relative w-full max-w-xl">
      <div className="relative">
        <input
          id={inputId}
          type="text"
          value={q}
          role="combobox"
          aria-expanded={open && items.length > 0}
          aria-controls={listboxId}
          aria-autocomplete="list"
          aria-haspopup="listbox"
          aria-activedescendant={
            activeIndex >= 0 ? `searchbar-option-${activeIndex}` : undefined
          }
          // iter 75: aria-busy 让屏幕阅读器把 "搜索中..." 关联到 input 本身
          // (之前的 role="status" span 是独立元素, AT 不一定关联)
          aria-busy={loading}
          aria-label="搜索股票代码或名称"
          onChange={(e) => setQ(e.target.value)}
          onFocus={() => q.trim() && items.length > 0 && setOpen(true)}
          onKeyDown={handleInputKeyDown}
          placeholder="搜索股票(代码或名称, 例: 600519、AAPL、腾讯)"
          className="w-full px-4 py-2.5 pl-10 bg-panel border border-border rounded-lg
                     focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500
                     focus:border-blue-500 text-slate-100 placeholder-slate-500"
        />
        <svg
          className="absolute left-3 top-3 w-5 h-5 text-slate-400"
          fill="none" stroke="currentColor" viewBox="0 0 24 24"
          aria-hidden="true"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
            d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
        </svg>
        {loading && (
          <span
            className="absolute right-3 top-3 text-xs text-slate-400"
            role="status"
            aria-live="polite"
          >
            搜索中...
          </span>
        )}
      </div>

      {open && items.length > 0 && (
        <ul
          id={listboxId}
          role="listbox"
          aria-label="股票搜索结果"
          className="absolute z-50 w-full mt-1 bg-panel border border-border rounded-lg shadow-2xl
                          max-h-80 overflow-y-auto"
        >
          {items.map((item, idx) => (
            <li
              key={`${item.market}-${item.code}`}
              id={`searchbar-option-${idx}`}
              role="option"
              aria-selected={idx === activeIndex}
              onClick={() => {
                onSelect(item);
                setOpen(false);
                const label = `${item.name} (${item.code})`;
                lastSelectedLabelRef.current = label;
                setQ(label);
                setActiveIndex(-1);
              }}
              className={`w-full px-4 py-2.5 flex items-center gap-3 cursor-pointer transition-colors text-left border-b border-border last:border-b-0 ${
                idx === activeIndex ? 'bg-slate-700' : 'hover:bg-slate-700'
              }`}
            >
              <span className={`${marketColor(item.market)} text-white text-xs px-2 py-0.5 rounded`}>
                {marketLabel(item.market)}
              </span>
              <span className="font-mono text-slate-200">{item.code}</span>
              <span className="text-slate-400 flex-1 truncate">{item.name}</span>
            </li>
          ))}
        </ul>
      )}

      {open && !loading && items.length === 0 && q.trim() && !error && (
        <div
          role="status"
          aria-live="polite"
          // iter 101: 加 aria-label — role=status + aria-live=polite 已能触发宣告,
          // 但屏幕阅读器在快速输入时可能跳过 polite。显式 label 提升可访问性。
          aria-label="无搜索结果"
          className="absolute z-50 w-full mt-1 bg-panel border border-border rounded-lg p-4 text-slate-400 text-sm"
        >
          未找到匹配 "{q}"
        </div>
      )}

      {error && (
        <div
          role="alert"
          aria-live="assertive"
          className="absolute z-50 w-full mt-1 bg-red-900/30 border border-red-800/60 rounded-lg p-3 text-red-300 text-sm"
        >
          {/* iter 109: toUserMessage 已经返回完整提示, 不再重复 "搜索出错:" 前缀 */}
          {error}
        </div>
      )}
    </div>
  );
});