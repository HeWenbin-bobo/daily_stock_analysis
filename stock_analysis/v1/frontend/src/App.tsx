import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { DaysSelector } from './components/DaysSelector';
import { ErrorBoundary } from './components/ErrorBoundary';
import { IndicatorChart } from './components/IndicatorChart';
import { KLineChart } from './components/KLineChart';
import { KLineChartSkeleton, PeakTroughSkeleton } from './components/Skeletons';
import { MockBanner } from './components/MockBanner';
import { PeakTroughPanel } from './components/PeakTroughPanel';
import { PeriodSelector } from './components/PeriodSelector';
import { SearchBar } from './components/SearchBar';
import { Watchlist } from './components/Watchlist';
import { analyzePeakTrough, fetchHistory, fetchIndicator, isCancelError } from './api/client';
import { useAppStore } from './hooks/useAppStore';
import { useTheme } from './hooks/useTheme';
import { useWatchlist } from './hooks/useWatchlist';
import { toUserMessage } from './utils/errors';
import { marketLabel } from './utils/market';
import type { IndicatorPoint, PeakTroughResponse, Period, StockHistoryResponse } from './types';

// iter 69: 模块级常量 — 避免每次 render 都重建数组/对象, 让 memo 包装的子组件能正确识别 props 稳定
const SAMPLE_STOCKS: ReadonlyArray<{ code: string; name: string; market: 'cn' | 'hk' | 'us' }> = [
  { code: '600519', name: '贵州茅台', market: 'cn' },
  { code: '300750', name: '宁德时代', market: 'cn' },
  { code: '00700', name: '腾讯控股', market: 'hk' },
  { code: 'AAPL', name: 'Apple', market: 'us' },
  { code: 'TSLA', name: 'Tesla', market: 'us' },
];

const INDICATOR_OPTIONS = [
  { key: 'boll' as const, label: 'BOLL', color: 'bg-amber-500' },
  { key: 'macd' as const, label: 'MACD', color: 'bg-blue-500' },
  { key: 'kdj' as const, label: 'KDJ', color: 'bg-purple-500' },
];

/**
 * 应用根组件 — 管理股票 K线与波峰波谷分析主流程。
 *
 * 状态管理: 使用 Zustand store (useAppStore) 存储 selectedStock/period/days，
 * 所有子组件通过 props 接收数据而非直接订阅 store，避免不必要的重渲染。
 *
 * 请求取消守卫: reqTokenRef (generation token) + lastAbortRef (AbortController) 组合
 * 防止竞态 — 快速切换股票时旧请求的响应不会覆盖新状态。组件卸载时也会 abort 最后一轮请求。
 *
 * 在线监听: useEffect 订阅 window 'online' 事件，从离线恢复时自动重拉数据
 * (iter 78)，避免用户以为数据已更新。
 *
 * 指标非阻塞模式: BOLL/MACD/KDJ 通过 Promise.then() 异步更新状态，失败不影响主流程。
 * 即使指标请求失败，用户仍能看到 K线和波峰波谷数据。
 */
function App() {
  const { theme, toggle } = useTheme();
  const { selectedStock, period, days, setSelectedStock, setPeriod, setDays } = useAppStore();
  const watchlist = useWatchlist();

  const [history, setHistory] = useState<StockHistoryResponse | null>(null);
  const [analysis, setAnalysis] = useState<PeakTroughResponse | null>(null);
  const [boll, setBoll] = useState<{ mid?: IndicatorPoint[]; upper?: IndicatorPoint[]; lower?: IndicatorPoint[] } | null>(null);
  const [macd, setMacd] = useState<{ dif?: IndicatorPoint[]; dea?: IndicatorPoint[]; hist?: IndicatorPoint[] } | null>(null);
  const [kdj, setKdj] = useState<{ k?: IndicatorPoint[]; d?: IndicatorPoint[]; j?: IndicatorPoint[] } | null>(null);
  const [showIndicators, setShowIndicators] = useState<{ boll: boolean; macd: boolean; kdj: boolean }>({ boll: false, macd: true, kdj: false });
  const [loadingHistory, setLoadingHistory] = useState(false);
  const [loadingAnalysis, setLoadingAnalysis] = useState(false);
  const [error, setError] = useState<string>('');
  // iter 157: mock banner 关闭状态 — 用户切到其它股票前, 不希望被旧 fallback 提示干扰。
  // 默认 true (banner 可见), 点击关闭后 false 直到切换股票。
  const [mockBannerDismissed, setMockBannerDismissed] = useState(false);

  // 修复 CRITICAL race condition: 用 generation token 标识请求, 旧响应不会覆盖新状态
  // 配合 AbortController 主动取消旧请求
  const reqTokenRef = useRef(0);
  // 保存上一次 refresh 的 AbortController, 在新 refresh 开始时主动 abort, 防网络浪费 + 旧逻辑回写
  const lastAbortRef = useRef<AbortController | null>(null);

  /**
   * 拉取 K线 + 波峰波谷 + 指标数据。
   *
   * 竞态守卫: 先递增 reqTokenRef.current 获取 generation token，
   * 请求完成后检查 token !== reqTokenRef.current 则丢弃响应，
   * 确保快速切换时旧响应不会覆盖新状态。
   *
   * AbortController: 每轮 refresh 创建新 controller 并存入 lastAbortRef，
   * 新一轮开始时调用 lastAbortRef.current?.abort() 取消上一轮 in-flight 请求，
   * 避免网络资源浪费和旧逻辑回写。
   *
   * 指标 (BOLL/MACD/KDJ): 非阻塞异步更新，失败仅在 DEV 模式 console.warn，
   * 主流程 (K线 + 波峰波谷) 独立处理，互不影响。
   */
  const refresh = useCallback(async () => {
    if (!selectedStock) return;
    const token = ++reqTokenRef.current;
    // 取消上一轮未完成的请求, 防止 dangling in-flight 响应回来覆盖新状态
    lastAbortRef.current?.abort();
    const ctrl = new AbortController();
    lastAbortRef.current = ctrl;

    setLoadingHistory(true);
    setError('');
    try {
      const h = await fetchHistory(selectedStock.code, period, days, ctrl.signal);
      // 检查是否被新请求取代
      if (token !== reqTokenRef.current) return;
      setHistory(h);

      setLoadingAnalysis(true);
      try {
        const a = await analyzePeakTrough(selectedStock.code, period, days, ctrl.signal);
        if (token !== reqTokenRef.current) return;
        setAnalysis(a);
      } catch (err: unknown) {
        if (token !== reqTokenRef.current || isCancelError(err)) return;
        setError(`波峰波谷分析失败: ${toUserMessage(err)}`);
        setAnalysis(null);
      } finally {
        if (token === reqTokenRef.current) setLoadingAnalysis(false);
      }

      // BOLL 指标(非阻塞, 失败不影响主流程)
      fetchIndicator(selectedStock.code, "BOLL", period, days, ctrl.signal)
        .then((ind) => {
          // iter 92: 双重守卫 — token 比较防过期响应, signal 检查防卸载中触发 setState
          if (ctrl.signal.aborted || token !== reqTokenRef.current) return;
          setBoll({
            mid: ind.series.mid,
            upper: ind.series.upper,
            lower: ind.series.lower,
          });
        })
        .catch((err) => {
          if (!isCancelError(err)) {
            if (import.meta.env.DEV) {
              console.warn("BOLL 指标获取失败:", err);
            }
            if (token === reqTokenRef.current) setBoll(null);
          }
        });

      // MACD 指标(非阻塞, 失败不影响主流程)
      fetchIndicator(selectedStock.code, "MACD", period, days, ctrl.signal)
        .then((ind) => {
          if (ctrl.signal.aborted || token !== reqTokenRef.current) return;
          setMacd({
            dif: ind.series.dif,
            dea: ind.series.dea,
            hist: ind.series.hist,
          });
        })
        .catch((err) => {
          if (!isCancelError(err)) {
            if (import.meta.env.DEV) {
              console.warn("MACD 指标获取失败:", err);
            }
            if (token === reqTokenRef.current) setMacd(null);
          }
        });

      // KDJ 指标(非阻塞, 失败不影响主流程)
      fetchIndicator(selectedStock.code, "KDJ", period, days, ctrl.signal)
        .then((ind) => {
          if (ctrl.signal.aborted || token !== reqTokenRef.current) return;
          setKdj({
            k: ind.series.k,
            d: ind.series.d,
            j: ind.series.j,
          });
        })
        .catch((err) => {
          if (!isCancelError(err)) {
            if (import.meta.env.DEV) {
              console.warn("KDJ 指标获取失败:", err);
            }
            if (token === reqTokenRef.current) setKdj(null);
          }
        });
    } catch (err: unknown) {
      if (token !== reqTokenRef.current || isCancelError(err)) return;
      setError(`获取 K线失败: ${toUserMessage(err)}`);
      setHistory(null);
      setAnalysis(null);
    } finally {
      if (token === reqTokenRef.current) setLoadingHistory(false);
    }
  }, [selectedStock, period, days]);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // iter 74: 切换股票/周期/天数时立即清空 error — 避免上一次失败的红色 banner
  // 在新请求成功响应回来之前误导用户
  // iter 157: 同步重置 mockBannerDismissed — 切换股票时新 banner 应再次可见
  useEffect(() => {
    setError('');
    setMockBannerDismissed(false);
  }, [selectedStock, period, days]);

  // iter 78: 监听浏览器 online 事件 — 从离线恢复时自动重拉数据
  // (网络故障期间缓存数据可能过期, 用户也常期待"网络恢复就能看到新数据")
  useEffect(() => {
    const onOnline = () => {
      void refresh();
    };
    window.addEventListener('online', onOnline);
    return () => {
      window.removeEventListener('online', onOnline);
    };
  }, [refresh]);

  // 卸载时清理: 取消最后一轮未完成请求, 释放 listener
  useEffect(() => {
    return () => {
      lastAbortRef.current?.abort();
      lastAbortRef.current = null;
    };
  }, []);

  // iter 76: useCallback 稳定 handleSelect 引用 — 与 SearchBar 内 useCallback handler 契约一致
  // 避免后续 memo(SearchBar) 时每次 render 重新触发 onSelect prop 比较
  /**
   * 切换当前选中股票 — 更新 Zustand store，触发 useEffect 重新拉取数据。
   *
   * @param s - 股票信息 { code, name, market }，来源为 SearchBar 搜索结果或示例股票
   */
  const handleSelect = useCallback(
    (s: { code: string; name: string; market: 'cn' | 'hk' | 'us' }) => {
      setSelectedStock(s);
    },
    [setSelectedStock],
  );

  // iter 99: extract toggleIndicator 回调 — 之前 INDICATOR_OPTIONS.map 里的内联
  // () => setShowIndicators(...) 每次 render 都新建箭头函数, 即使子按钮没 memo
  // 也会触发不必要的子 render。提取后稳定引用。
  /**
   * 切换指标显示状态 (BOLL/MACD/KDJ)。
   *
   * @param key - 指标键名，对应 showIndicators 状态对象的属性
   *
   * 注意: 此回调提取为 useCallback (iter 99) 稳定引用，
   * 之前 INDICATOR_OPTIONS.map 内的内联箭头函数每次 render 都重建，
   * 即使按钮组件没变也会触发不必要的重渲染。
   */
  const toggleIndicator = useCallback(
    (key: 'boll' | 'macd' | 'kdj') => {
      setShowIndicators((prev) => ({ ...prev, [key]: !prev[key] }));
    },
    [],
  );

  // iter 79: memo secondLine 对象 — 之前每次 render 都新建 {data, color},
  // 让 memo 包了的 IndicatorChart 在 secondLine reference 变化时误以为 props 变了,
  // 进而触发 chart 重建 useEffect ([color, secondLine?.color, targetMode, height])。
  /**
   * MACD 副图的第二根线 (DEA) — memo 稳定引用，防止 IndicatorChart 误以为 props 变化。
   *
   * 使用 useMemo 而非每次 render 新建对象: 若 secondLine reference 变化，
   * IndicatorChart 的 useEffect ([color, secondLine?.color, targetMode, height])
   * 会误触发 chart 重建 (iter 79)。
   *
   * @returns 存在 MACD 数据时返回 { data, color }，否则 undefined
   */
  const macdSecondLine = useMemo(
    () => (macd?.dea ? { data: macd.dea, color: '#a78bfa' } : undefined),
    [macd?.dea],
  );
  /**
   * KDJ 副图的 D 线 — memo 稳定引用，同 macdSecondLine。
   *
   * @returns 存在 KDJ 数据时返回 { data, color }，否则 undefined
   */
  const kdjDSecondLine = useMemo(
    () => (kdj?.d ? { data: kdj.d, color: '#60a5fa' } : undefined),
    [kdj?.d],
  );

  // iter 82: extract watchlist.has() before render — 之前 JSX 内 3 次调用触发 zustand
  // 订阅, 现在 1 次。
  const inWatchlist = selectedStock
    ? watchlist.has(selectedStock.code, selectedStock.market)
    : false;

  return (
    // iter 64: 顶层 ErrorBoundary — 任何子组件 render 异常都能降级到提示页 + 重试,
    // 而不是白屏 (CRITICAL resilience fix)
    <ErrorBoundary>
      <div className="min-h-screen flex flex-col">
        {/* 顶栏 */}
        <header className="border-b border-border bg-panel/80 backdrop-blur sticky top-0 z-40">
        <div className="max-w-screen-2xl mx-auto px-4 sm:px-6 py-3 flex items-center gap-3 sm:gap-6 flex-wrap">
          <div className="flex items-center gap-2">
            <div className="w-8 h-8 bg-gradient-to-br from-blue-500 to-purple-600 rounded-lg flex items-center justify-center text-white font-bold">
              K
            </div>
            <div className="hidden sm:block">
              <h1 className="text-lg font-semibold text-slate-100 leading-tight">
                个股 K线 + 波峰波谷分析
              </h1>
              <p className="text-xs text-slate-400 leading-tight">
                A股 · 港股 · 美股 · 日/周/月 K线
              </p>
            </div>
          </div>
          {/* iter 68: 跳到主要内容链接 — WCAG 2.4.1 Bypass Blocks */}
          <a
            href="#main-content"
            className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-50
                       focus:px-3 focus:py-2 focus:bg-blue-600 focus:text-white focus:rounded
                       focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
          >
            跳到主要内容
          </a>
          <div className="flex-1 min-w-[200px] sm:min-w-[260px] max-w-xl order-3 sm:order-2 basis-full sm:basis-auto">
            <SearchBar onSelect={handleSelect} />
          </div>
          <div className="order-2 sm:order-3 flex items-center gap-2 sm:gap-3">
            <PeriodSelector value={period} onChange={(p: Period) => setPeriod(p)} />
            <DaysSelector value={days} onChange={setDays} />
            <button
            type="button"
            onClick={toggle}
            // iter 102: 加 aria-pressed — 与 PeriodSelector/DaysSelector/indicator toggles 一致,
            // 屏幕阅读器需要知道当前是哪个状态 (binary toggle)。
            aria-pressed={theme === 'dark'}
            className="p-2 rounded-lg bg-panel border border-border hover:bg-slate-700
                       text-slate-300 hover:text-yellow-300 transition-colors
                       focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500"
            title={`切换到${theme === 'dark' ? '亮色' : '暗色'}主题`}
            aria-label="切换主题"
          >
            {theme === 'dark' ? (
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
              </svg>
            ) : (
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
              </svg>
            )}
          </button>
          </div>
        </div>
      </header>

      {/* 主区 */}
      <main id="main-content" className="flex-1 max-w-screen-2xl w-full mx-auto px-6 py-6">
        {!selectedStock && (
          <div className="flex flex-col items-center justify-center h-[60vh] text-slate-400">
            <div className="text-6xl mb-4" aria-hidden="true">📈</div>
            <h2 className="text-xl font-semibold text-slate-400 mb-2">开始一次分析</h2>
            <p className="text-sm">在上方搜索框输入股票代码或名称</p>
            <div
              className="mt-6 flex gap-2 flex-wrap justify-center"
              // iter 109: role="group" + aria-label — 让屏幕阅读器知道这是一个
              // "快速选择示例股票"的按钮组, 而不是孤立的几个按钮
              role="group"
              aria-label="快速选择示例股票"
            >
              {SAMPLE_STOCKS.map((s) => (
                <button
                  key={s.code}
                  type="button"
                  onClick={() => handleSelect(s)}
                  className="px-3 py-1.5 bg-panel border border-border rounded-lg
                             text-sm text-slate-300 hover:bg-slate-700 hover:border-blue-500 transition-colors"
                >
                  {s.name} ({s.code})
                </button>
              ))}
            </div>
          </div>
        )}

        {watchlist.items.length > 0 && !selectedStock && (
          <div className="flex justify-center w-full">
            <Watchlist
              items={watchlist.items}
              onSelect={(item) => setSelectedStock(item)}
              onRemove={(code, market) => watchlist.remove(code, market)}
              onImport={(items) => items.forEach((it) => watchlist.add(it))}
            />
          </div>
        )}

        {selectedStock && (
          <div className="space-y-6">
            {/* 股票标题 */}
            <div className="flex items-center gap-4 flex-wrap">
              <div className="flex-1">
                <h2 className="text-2xl font-bold text-slate-100 flex items-center gap-2">
                  {/* iter 165: 加载中显示 selectedStock.name, 避免旧 history.stock_name 与新代码
                  混合 (e.g. "Apple Inc. 600519")。iter 161 只修了副标题, 标题漏了。 */}
                  {loadingHistory ? selectedStock.name : (history?.stock_name ?? selectedStock.name)}
                  <span className="text-base text-slate-400 font-mono font-normal">
                    {selectedStock.code}
                  </span>
                  <button
                    type="button"
                    onClick={() => {
                      if (inWatchlist) {
                        watchlist.remove(selectedStock.code, selectedStock.market);
                      } else {
                        watchlist.add(selectedStock);
                      }
                    }}
                    title={inWatchlist ? '从自选股移除' : '加入自选股'}
                    aria-label="切换自选股状态"
                    className="ml-1 p-1.5 rounded hover:bg-slate-700 transition-colors"
                  >
                    <svg
                      className={`w-5 h-5 ${inWatchlist ? 'text-yellow-400 fill-yellow-400' : 'text-slate-500'}`}
                      fill={inWatchlist ? 'currentColor' : 'none'}
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                        d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.197-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.784-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z" />
                    </svg>
                  </button>
                </h2>
                <p className="text-sm text-slate-400">
                  {/* iter 113: 用 marketLabel 而非内联三元 — 与 Watchlist/SearchBar
                  保持一致, 单一来源。marketLabel 已从 utils/market 导出 */}
                  {marketLabel(selectedStock.market)}
                  {' · '}
                  {period === 'daily' ? '日 K' : period === 'weekly' ? '周 K' : '月 K'}
                  {/* iter 161: 加载中不显示上一只股票的旧统计 (bar 数 + 来源)。
                  切换股票时 history 仍是旧值, 之前会短暂显示 "365 根 K线 · 来源 akshare"
                  但实际是新股票在加载 (见 dogfood QA)。加载中显示 "加载中...", 避免误导。 */}
                  {' · '}
                  {loadingHistory ? (
                    '加载中...'
                  ) : (
                    <>
                      {history?.data.length ?? 0} 根 K线
                      {history?.source && ` · 来源 ${history.source}`}
                    </>
                  )}
                </p>
              </div>
              {/* iter 78: 手动刷新按钮 — 自动刷新失效或想跳过缓存时主动拉新数据 */}
              <button
                type="button"
                onClick={() => {
                  void refresh();
                }}
                disabled={loadingHistory}
                title="刷新数据"
                aria-label="刷新数据"
                className="p-2 rounded-lg bg-panel border border-border hover:bg-slate-700
                           text-slate-300 hover:text-blue-300 transition-colors
                           focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500
                           // iter 137: 用 disabled:text-slate-500 替代 disabled:opacity-50 —
                           // opacity-50 让 text-slate-300 变成 ~50% 透明, 对比度降到 ~2.3:1,
                           // 不达 WCAG AA (4.5:1)。slate-500 在 dark bg 上保持 ~4.6:1。
                           disabled:text-slate-500 disabled:cursor-not-allowed disabled:hover:bg-panel"
              >
                <svg
                  className={`w-5 h-5 ${loadingHistory ? 'animate-spin' : ''}`}
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                  aria-hidden="true"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
                  />
                </svg>
              </button>
              {history && (history.source === 'mock' || history.source === 'mock_fallback') && !mockBannerDismissed && (
                <div className="mt-2">
                  <MockBanner
                    source={history.source}
                    onRetry={() => {
                      void refresh();
                    }}
                    onDismiss={() => setMockBannerDismissed(true)}
                  />
                </div>
              )}
            </div>

            {error && (
              <div
                className="bg-red-900/30 border border-red-800/60 text-red-300 rounded-lg p-3 text-sm flex items-center justify-between gap-3"
                role="alert"
                aria-live="assertive"
              >
                <span>{error}</span>
                <div className="flex items-center gap-2">
                  {/* iter 70: 主错误 banner 加"重试"按钮, 让用户不用刷新就能恢复 — 与 PeakTroughPanel / ErrorBoundary 模式一致 */}
                  <button
                    type="button"
                    onClick={() => {
                      setError('');
                      void refresh();
                    }}
                    className="px-3 py-1 bg-red-700 hover:bg-red-600 text-white text-xs rounded transition-colors
                               focus:outline-none focus-visible:ring-2 focus-visible:ring-red-400"
                    aria-label="重试获取数据"
                  >
                    重试
                  </button>
                  {/* iter 102: 加 dismiss 按钮 — 用户切到其它股票时不应被旧错误 banner 干扰,
                  且刷新失败后不必一直看到这条 message。Watchlist import 已有同样模式。 */}
                  <button
                    type="button"
                    onClick={() => setError('')}
                    className="p-1 text-slate-400 hover:text-slate-200 rounded
                               focus:outline-none focus-visible:ring-2 focus-visible:ring-red-400"
                    aria-label="关闭错误提示"
                  >
                    <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                        d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              </div>
            )}

            {/* K线图 */}
            <div className="bg-panel border border-border rounded-lg p-3" style={{ minHeight: 480 }}>
              {loadingHistory ? (
                <KLineChartSkeleton />
              ) : history && history.data.length > 0 ? (
                // 子树 ErrorBoundary: 图表底层库抛错时只丢图表, 不致整页崩溃 (iter 66)
                <ErrorBoundary fallback={<div className="h-full flex items-center justify-center text-slate-400 text-sm">K 线图渲染失败, 请刷新或换一只股票</div>}>
                  <KLineChart
                    data={history.data}
                    market={history.market}
                    segments={analysis?.segments}
                    yearHigh={analysis?.year_high}
                    boll={boll}
                  />
                </ErrorBoundary>
              ) : (
                <div className="flex items-center justify-center h-full text-slate-400">
                  暂无数据
                </div>
              )}
            </div>

            {/* 指标切换器 */}
            <div className="flex items-center gap-2 flex-wrap bg-panel/50 border border-border/60 rounded-lg px-3 py-2">
              <span className="text-xs text-slate-400 mr-1">叠加指标:</span>
              {INDICATOR_OPTIONS.map((opt) => (
                <button
                  key={opt.key}
                  type="button"
                  onClick={() => toggleIndicator(opt.key)}
                  className={`px-3 py-1 text-xs rounded transition-colors flex items-center gap-1.5 ${
                    showIndicators[opt.key]
                      ? "bg-slate-700 text-slate-100 border border-slate-600"
                      : "bg-slate-800/40 text-slate-500 hover:text-slate-300 border border-transparent"
                  }`}
                  title={`${showIndicators[opt.key] ? "隐藏" : "显示"} ${opt.label}`}
                  aria-pressed={showIndicators[opt.key]}
                  // iter 112: aria-label 加上动态动作 — 仅 aria-pressed 不够, 屏幕阅读器
                  // (NVDA/JAWS) 不会自动读 "pressed/not pressed", 用户听到的还是
                  // "BOLL"。title 不可靠 — SR 不一定读, iOS Safari 也不读。
                  aria-label={`${showIndicators[opt.key] ? "隐藏" : "显示"} ${opt.label} 指标`}
                >
                  <span className={`w-2 h-2 rounded-full ${opt.color}`} aria-hidden="true"></span>
                  {opt.label}
                </button>
              ))}
            </div>

            {/* MACD 副图 - DIF / DEA / HIST 三合一 */}
            {/* iter 119: 单独 ErrorBoundary — 之前 MACD/KDJ 共享 PeakTroughPanel 边界 (line 566),
            一个图表底层库抛错会让 BOLL+MACD+KDJ 全部消失。KLineChart 已有独立边界 (line 457),
            IndicatorChart 应当一致, 每个图表单独降级。 */}
            {showIndicators.macd && macd && macd.hist && macd.hist.length > 0 && (
              <ErrorBoundary
                fallback={
                  <div className="bg-panel border border-border rounded-lg p-3 text-slate-400 text-sm">
                    MACD 图表渲染失败, 请刷新或换一只股票
                  </div>
                }
              >
                <div className="bg-panel border border-border rounded-lg p-3">
                  {/* iter 105: 加 aria-label — 图例是视觉装饰, 屏幕阅读器需要知道是哪个指标的图例 */}
                  <div className="flex items-center gap-4 mb-1 text-xs text-slate-400" aria-label="MACD 指标图例">
                    <span className="font-semibold text-slate-300">MACD (12, 26, 9)</span>
                    <span className="flex items-center gap-1">
                      <span className="w-3 h-0.5 bg-yellow-400 inline-block"></span>
                      DIF
                    </span>
                    <span className="flex items-center gap-1">
                      <span className="w-3 h-0.5 bg-purple-400 inline-block"></span>
                      DEA
                    </span>
                    <span className="flex items-center gap-1">
                      <span className="w-3 h-2 bg-emerald-500 inline-block"></span>
                      HIST
                    </span>
                  </div>
                  <IndicatorChart
                    data={macd.dif ?? []}
                    color="#fbbf24"
                    secondLine={macdSecondLine}
                    histogramData={macd.hist ?? []}
                    ariaLabelSuffix="MACD"
                  />
                </div>
              </ErrorBoundary>
            )}

            {/* KDJ 副图 - K 与 D 一图, J 单独一图 */}
            {showIndicators.kdj && kdj && kdj.k && kdj.k.length > 0 && (
              <ErrorBoundary
                fallback={
                  <div className="bg-panel border border-border rounded-lg p-3 text-slate-400 text-sm">
                    KDJ 图表渲染失败, 请刷新或换一只股票
                  </div>
                }
              >
                <div className="bg-panel border border-border rounded-lg p-3">
                  {/* iter 105: 加 aria-label — 与 MACD 图例一致 */}
                  <div className="flex items-center gap-4 mb-1 text-xs text-slate-400" aria-label="KDJ 指标图例">
                    <span className="font-semibold text-slate-300">KDJ (9, 3, 3)</span>
                    <span className="flex items-center gap-1">
                      <span className="w-3 h-0.5 bg-amber-400 inline-block"></span>
                      K
                    </span>
                    <span className="flex items-center gap-1">
                      <span className="w-3 h-0.5 bg-blue-400 inline-block"></span>
                      D
                    </span>
                    <span className="flex items-center gap-1">
                      <span className="w-3 h-0.5 bg-purple-400 inline-block"></span>
                      J
                    </span>
                    <span className="text-slate-400 ml-2">超买线 80 / 超卖线 20</span>
                  </div>
                  <IndicatorChart
                    data={kdj.k ?? []}
                    color="#fbbf24"
                    secondLine={kdjDSecondLine}
                    ariaLabelSuffix="KDJ-K"
                  />
                  <IndicatorChart
                    data={kdj.j ?? []}
                    color="#a78bfa"
                    ariaLabelSuffix="KDJ-J"
                  />
                </div>
              </ErrorBoundary>
            )}

            {/* 波峰波谷 */}
            {loadingAnalysis ? (
              <PeakTroughSkeleton />
            ) : analysis ? (
              <ErrorBoundary fallback={<div className="bg-panel border border-border rounded-lg p-4 text-slate-400 text-sm">波峰波谷面板渲染失败, 请刷新或换一只股票</div>}>
                <PeakTroughPanel
                  analysis={analysis}
                  stockCode={selectedStock.code}
                  period={period}
                  history={history?.data}
                  days={days}
                  source={history?.source}
                />
              </ErrorBoundary>
            ) : null}

            {/* 自选股栏 - 显示在选中股票视图下方, 方便切换 */}
            {watchlist.items.length > 1 && (
              <div className="mt-6 pt-6 border-t border-border">
                <Watchlist
                  items={watchlist.items}
                  onSelect={(item) => setSelectedStock(item)}
                  onRemove={(code, market) => watchlist.remove(code, market)}
                  onImport={(items) => items.forEach((it) => watchlist.add(it))}
                  currentCode={selectedStock.code}
                  currentMarket={selectedStock.market}
                />
              </div>
            )}
          </div>
        )}
      </main>

      <footer className="border-t border-border py-3 text-center text-xs text-slate-400">
        Data: akshare + yfinance · LLM: CC switch 当前激活 provider · 仅供学习参考
      </footer>
      </div>
    </ErrorBoundary>
  );
}

// iter 87: 移除 React.memo(App) — App 是根组件, 没有父级 props, memo 永远不会触发
// shallow compare 跳过渲染路径。原 memo 是 intent-documentation 性质, 改成纯注释。
// 真正的优化在子组件 (SearchBar/Watchlist/KLineChart/PeriodSelector/DaysSelector 已 memo)
// + 父级传稳定 callback (useCallback)。App 自身不该有 memo 包装。
export default App;