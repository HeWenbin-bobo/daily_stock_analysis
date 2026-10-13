/**
 * 波峰波谷分析面板。渲染年度高/低点和分段序列表格 (iter 113 dynamic aria-label),
 * 提供 AI 解读入口、CSV/JSON 导出按钮 (iter 100), LLM 状态指示。
 */
import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { KLineData, PeakTroughResponse } from '../types';
import { fetchLLMInsight, fetchLLMStatus, isCancelError } from '../api/client';
import { exportAnalysisAsJSON, exportHistoryAsCSV } from '../utils/exportData';
import { renderMarkdown } from '../utils/markdown';
import { toUserMessage } from '../utils/errors';

interface Props {
  analysis: PeakTroughResponse;
  stockCode: string;
  period: 'daily' | 'weekly' | 'monthly';
  /** 用于 CSV 导出的 K线原始数据 */
  history?: KLineData[];
  /** AI 解读使用的回看天数; 不传默认 365 */
  days?: number;
  /** iter 157: 数据源标签 — 透传给 CSV/JSON 导出, 让读者知道数据是 akshare / yfinance / mock / mock_fallback。
   *  不传则导出文件不带 source 字段, 保持向后兼容。 */
  source?: string;
}

// iter 84: 删除局部 toSafeErrorMessage — 直接用 toUserMessage(from utils/errors).
// utils/errors.toUserMessage 已经做了 cancel 检查 + axios 状态码归类 + 非 axios 错误通用 fallback。

// iter 88: 把 Stat 移到 PeakTroughPanel 之前 — 提高 co-location, 主要组件声明中
// 立即可见其依赖。改用 const (无 export 关键字), 因为只在文件内使用。
const Stat = memo(function Stat({ label, value, sub, accent }: { label: string; value: string; sub?: string; accent: string }) {
  return (
    <div className="bg-slate-800/40 border border-border/50 rounded-lg p-3">
      <div className="text-xs text-slate-400 mb-1">{label}</div>
      <div className={`text-lg font-semibold ${accent}`}>{value}</div>
      {sub && <div className="text-xs text-slate-400 mt-0.5 font-mono">{sub}</div>}
    </div>
  );
});

// React.memo 避免父组件因 BOLL/MACD/KDJ state 变化而白白重渲染 (iter 63 perf)
export const PeakTroughPanel = memo(function PeakTroughPanel({ analysis, stockCode, period, history, days = 365, source }: Props) {
  const [aiText, setAiText] = useState<string>('');
  const [aiMeta, setAiMeta] = useState<{ provider: string; model: string; source: string } | null>(null);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState<string>('');
  const [status, setStatus] = useState<{ available: boolean; provider: string | null; source: string } | null>(null);

  // AI 请求 generation + AbortController: 防止旧响应覆盖新响应 (切换 stock/period/days)
  const aiReqTokenRef = useRef(0);
  const aiAbortRef = useRef<AbortController | null>(null);

    // 修复 CRITICAL: 副作用放在 useEffect 里, 不要在 render body 内做网络请求
  useEffect(() => {
    // iter 91: 移除 `let cancelled = false` 标志 — AbortController 已覆盖同功能,
    // 简化代码。`.then`/`catch` 通过 `ctrl.signal.aborted` 守卫 + `isCancelError`
    // 双重保险避免 unmount 后 setState。
    const ctrl = new AbortController();
    fetchLLMStatus(ctrl.signal)
      .then((s) => {
        if (!ctrl.signal.aborted) {
          setStatus({ available: s.available, provider: s.provider, source: s.source });
        }
      })
      .catch((err: unknown) => {
        if (!isCancelError(err)) {
          setStatus({ available: false, provider: null, source: 'error' });
        }
      });
    return () => {
      ctrl.abort();
    };
  }, []);

  // 修复 HIGH: 切换股票/周期/天数时清空旧 AI 输出 + 取消未完成请求, 避免显示错位
  useEffect(() => {
    aiAbortRef.current?.abort();
    aiAbortRef.current = null;
    aiReqTokenRef.current++; // 让任何正在进行的 .then 自动失效
    setAiText('');
    setAiMeta(null);
    setAiError('');
    // 清空 loading 状态: 旧请求不会走 finally 分支(因 token 已过期), 必须手动复位按钮
    setAiLoading(false);
  }, [stockCode, period, days]);

  // 组件卸载时清理
  useEffect(() => {
    return () => {
      aiAbortRef.current?.abort();
      aiAbortRef.current = null;
    };
  }, []);

  // iter 69: 用 useCallback 稳定 onAiInsight 引用 — 虽然父组件未 memo 但保留契约清晰
  const onAiInsight = useCallback(async () => {
    // 取消任何正在进行的旧请求
    aiAbortRef.current?.abort();
    const ctrl = new AbortController();
    aiAbortRef.current = ctrl;
    const token = ++aiReqTokenRef.current;

    setAiLoading(true);
    setAiError('');
    try {
      const result = await fetchLLMInsight(stockCode, period, days, undefined, ctrl.signal);
      if (token !== aiReqTokenRef.current || ctrl.signal.aborted) return;
      // 防御: 后端 / 网络异常可能让 result.text 缺失或非字符串 (iter 63 test 修复)
      // iter 82: provider/model/source 也加 null 守卫 — 类型系统不强制, 后端可能在
      // 某些 fallback 路径下返回 null (如 cc_switch 失败但仍返回 200 的情况)
      const text = typeof result?.text === 'string' ? result.text : '';
      setAiText(text);
      setAiMeta({
        provider: typeof result?.provider === 'string' ? result.provider : '',
        model: typeof result?.model === 'string' ? result.model : '',
        source: typeof result?.source === 'string' ? result.source : '',
      });
    } catch (err: unknown) {
      if (token !== aiReqTokenRef.current || isCancelError(err)) return;
      // iter 84: 用 toUserMessage(from utils/errors) 但传 PeakTroughPanel 专属 fallback,
      // 保留之前 toSafeErrorMessage 的提示语 "请求失败, 请稍后重试"。
      const safe = toUserMessage(err, '请求失败, 请稍后重试');
      if (safe) setAiError(safe);
    } finally {
      if (token === aiReqTokenRef.current) setAiLoading(false);
    }
  }, [stockCode, period, days]);

  const { summary, segments, year_high } = analysis;

  // 缓存渲染的 HTML, 避免每次输入状态变化都重新解析
  const aiHtml = useMemo(() => renderMarkdown(aiText), [aiText]);

  return (
    <div className="bg-panel border border-border rounded-lg p-4 space-y-4">
      {/* 顶部摘要 */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        <Stat label="年度最高点" value={`${year_high.price?.toFixed(2) ?? '-'}`} sub={year_high.date} accent="text-yellow-400" />
        <Stat label="当前价" value={`${summary.current_price?.toFixed(2) ?? '-'}`} accent="text-blue-400" />
        <Stat label="年初价" value={`${summary.year_start_price?.toFixed(2) ?? '-'}`} accent="text-slate-300" />
        <Stat label="年初至今" value={`${summary.current_vs_year_start_pct?.toFixed(2) ?? 0}%`}
              accent={(summary.current_vs_year_start_pct ?? 0) >= 0 ? 'text-emerald-300' : 'text-red-300'} />
        <Stat label="距年度高点" value={`${summary.current_vs_high_pct?.toFixed(2) ?? 0}%`}
              accent={(summary.current_vs_high_pct ?? 0) <= 0 ? 'text-emerald-300' : 'text-red-300'} />
        {/* 颜色语义: "距年度高点" 是绝对距离, 0 或负=创新高(绿); 正=落后高点(红)。
            (年初至今则反过来: 正=涨(绿), 负=跌(红)。) */}
      </div>

      {/* 波峰波谷表 */}
      <div>
        <h3 className="text-sm font-semibold text-slate-300 mb-2">
          波峰波谷序列 (从年度高点开始)
        </h3>
        {segments.length === 0 ? (
          <p className="text-slate-400 text-sm">本年度高点之后数据不足, 无可分析段。</p>
        ) : (
          <div className="overflow-x-auto">
            {/* iter 113: aria-label 加上股票名 — 与导出按钮 iter 100 模式一致, 让屏幕阅读器
            在切换不同股票后能区分是哪只股的波峰波谷表 (导出按钮已经动态含 stock_name)。 */}
            <table className="w-full text-sm" aria-label={`${analysis.stock_name} (${analysis.stock_code}) 波峰波谷分析数据`}>
              <thead className="text-slate-400 border-b border-border">
                <tr>
                  <th scope="col" className="text-left py-2 px-2">#</th>
                  <th scope="col" className="text-left py-2 px-2">类型</th>
                  <th scope="col" className="text-left py-2 px-2">起</th>
                  <th scope="col" className="text-left py-2 px-2">止</th>
                  <th scope="col" className="text-right py-2 px-2">起点价</th>
                  <th scope="col" className="text-right py-2 px-2">终点价</th>
                  <th scope="col" className="text-right py-2 px-2">变化%</th>
                </tr>
              </thead>
              <tbody>
                {segments.map((s, i) => (
                  <tr key={i} className="border-b border-border/50 hover:bg-slate-700/30">
                    <td className="py-2 px-2 text-slate-500">{i + 1}</td>
                    <td className="py-2 px-2">
                      <span
                        className={`px-2 py-0.5 rounded text-xs ${
                          s.type === 'decline'
                            ? 'bg-red-900/40 text-red-300'
                            : 'bg-emerald-900/40 text-emerald-300'
                        }`}
                      >
                        {s.type === 'decline' ? '回撤' : '回弹'}
                        {s.from_peak && ' · 自高点'}
                      </span>
                    </td>
                    <td className="py-2 px-2 text-slate-300 font-mono text-xs max-w-[110px] truncate" title={s.from_date}>{s.from_date}</td>
                    <td className="py-2 px-2 text-slate-300 font-mono text-xs max-w-[110px] truncate" title={s.to_date}>{s.to_date}</td>
                    <td className="py-2 px-2 text-right text-slate-300">{s.start_price?.toFixed(2) ?? '-'}</td>
                    <td className="py-2 px-2 text-right text-slate-300">{s.end_price?.toFixed(2) ?? '-'}</td>
                    <td
                      className={`py-2 px-2 text-right font-mono ${
                        s.change_pct >= 0 ? 'text-emerald-300' : 'text-red-300'
                      }`}
                    >
                      {s.change_pct >= 0 ? '+' : ''}{s.change_pct.toFixed(2)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* AI 解读 */}
      <div className="border-t border-border pt-3">
        <div className="flex items-center gap-3 mb-2 flex-wrap">
          <button
            type="button"
            onClick={onAiInsight}
            disabled={aiLoading || !status?.available}
            aria-busy={aiLoading}
            className="px-4 py-1.5 bg-blue-600 hover:bg-blue-700 disabled:bg-slate-700
                       disabled:cursor-not-allowed text-white text-sm rounded-lg transition-colors"
          >
            {aiLoading ? 'AI 解读中...' : '✨ AI 解读'}
          </button>
          {/* iter 68: 把加载状态也通过 aria-live 暴露给屏幕阅读器 (仅在 loading 时挂载) */}
          {aiLoading && (
            <span role="status" aria-live="polite" className="sr-only">
              AI 解读中
            </span>
          )}
          {history && (
            <button
              type="button"
              onClick={() =>
                exportHistoryAsCSV(
                  analysis.stock_code,
                  analysis.stock_name,
                  analysis.period,
                  history,
                  analysis.segments,
                  source
                )
              }
              className="px-3 py-1.5 bg-slate-700 hover:bg-slate-600 text-slate-100
                         text-sm rounded-lg transition-colors flex items-center gap-1.5"
              title="导出 K线 + 波峰波谷为 CSV (Excel 可直接打开)"
              // iter 100: 动态 aria-label — 屏幕阅读器可分辨是哪只股票, 不再笼统 "K线数据"。
              aria-label={`导出 ${analysis.stock_name} (${analysis.stock_code}) K线数据为 CSV 文件`}
            >
              <svg className="w-4 h-4" aria-hidden="true" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
              </svg>
              CSV
            </button>
          )}
          {history && (
            <button
              type="button"
              onClick={() => exportAnalysisAsJSON(analysis, history, source)}
              className="px-3 py-1.5 bg-slate-700 hover:bg-slate-600 text-slate-100
                         text-sm rounded-lg transition-colors flex items-center gap-1.5"
              title="导出完整分析为 JSON"
              // iter 100: 动态 aria-label — 同样让屏幕阅读器知道是哪只股票。
              aria-label={`导出 ${analysis.stock_name} (${analysis.stock_code}) 完整分析结果为 JSON 文件`}
            >
              <svg className="w-4 h-4" aria-hidden="true" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4" />
              </svg>
              JSON
            </button>
          )}
          {status && (
            <span className="text-xs text-slate-400">
              {status.available
                ? `当前 LLM: ${status.provider} (${status.source})`
                // iter 102: 不再泄露 "CC switch" / ".env" — 普通用户不需要知道
                // 这些内部架构细节, F12 也不应暴露。仅在 DEV 时显示完整配置提示。
                : import.meta.env.DEV
                  ? '未配置 LLM (在 CC switch 启用 provider 或在 backend/.env 配置 key)'
                  : 'LLM 暂不可用'}
            </span>
          )}
        </div>
        {aiError && (
          <div
            role="alert"
            aria-live="assertive"
            className="text-red-400 text-sm bg-red-900/20 border border-red-900/40 rounded p-3 flex items-center justify-between gap-3"
          >
            <span>
              {/* iter 103: aria-hidden — 警告 emoji 是装饰性, 不应被屏幕阅读器念出
              (e.g. "warning sign exclamation" 干扰上下文)。其余装饰性 emoji 同样处理。 */}
              <span aria-hidden="true">⚠️</span> {aiError}
            </span>
            <button
              type="button"
              onClick={onAiInsight}
              aria-label="重试 AI 解读"
              className="px-3 py-1 bg-red-700 hover:bg-red-600 text-white text-xs rounded transition-colors"
            >
              重试
            </button>
          </div>
        )}
        {aiText && (
          <>
            <div
              className="bg-slate-800/50 rounded-lg p-3 text-sm text-slate-200 leading-relaxed [&_p]:my-2 [&_h1]:text-xl [&_h1]:font-bold [&_h1]:mt-3 [&_h1]:mb-2 [&_h2]:text-lg [&_h2]:font-semibold [&_h2]:mt-3 [&_h2]:mb-1.5 [&_h3]:text-base [&_h3]:font-semibold [&_h3]:mt-2 [&_h3]:mb-1 [&_ul]:list-disc [&_ul]:list-inside [&_ul]:my-2 [&_ul]:space-y-1 [&_ol]:list-decimal [&_ol]:list-inside [&_ol]:my-2 [&_ol]:space-y-1 [&_blockquote]:border-l-4 [&_blockquote]:border-blue-500/50 [&_blockquote]:pl-3 [&_blockquote]:my-2 [&_blockquote]:italic [&_code]:px-1 [&_code]:py-0.5 [&_code]:bg-slate-700/60 [&_code]:rounded [&_code]:font-mono"
              dangerouslySetInnerHTML={{ __html: aiHtml }}
            />
            {aiMeta && (
              <div className="mt-2 pt-2 border-t border-border/50 text-xs text-slate-400">
                — {aiMeta.provider} / {aiMeta.model} (来源: {aiMeta.source})
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
});