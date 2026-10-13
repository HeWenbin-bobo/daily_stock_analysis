import type { DataSource } from '../types';

/**
 * Mock 数据 / 上游失败提示 banner。
 *
 * iter 157: 之前 App.tsx 里只有 source === 'mock' 才会显示 banner,
 * 当 yfinance 被限流而代码静默 fallback 到 mock 时, 用户看到的依然是
 * AAPL 180 起步的假数据而毫无察觉。现在扩展到 mock_fallback, 并抽出
 * 为独立组件以便单测。
 *
 * 可访问性:
 *  - role="status" + aria-live="polite" 让屏幕阅读器在数据更新时朗读
 *  - 提供 "重试" 按钮: 用户不需要刷新整页就能再尝试一次
 *  - 提供关闭按钮: 切到其它股票时不应被旧 banner 干扰 (与 PeakTroughPanel error 模式一致)
 */

interface MockBannerProps {
  source: DataSource | string;
  onRetry?: () => void;
  onDismiss?: () => void;
}

/** 根据 source 返回 (颜色, 标题, 详细描述) 三元组。 */
function describeMockSource(source: DataSource | string): {
  tone: 'amber' | 'rose';
  title: string;
  description: string;
} {
  if (source === 'mock_fallback') {
    return {
      tone: 'rose',
      title: '数据源暂时不可用,已回退到模拟数据',
      description:
        'akshare 或 yfinance 上游请求失败 (常见原因: 限流、网络中断、API 临时不可用)。当前显示的不是真实行情, 请稍后点击「重试」或换一只股票。',
    };
  }
  // source === 'mock' (用户主动选择)
  return {
    tone: 'amber',
    title: '演示数据(非实时)',
    description:
      '当前网络环境无法访问 akshare 或 yfinance, 显示的是基于股票代码生成的确定性 mock 数据, 走势与真实行情无关。',
  };
}

export function MockBanner({ source, onRetry, onDismiss }: MockBannerProps) {
  if (source !== 'mock' && source !== 'mock_fallback') return null;

  const { tone, title, description } = describeMockSource(source);
  // amber (主动 mock) vs rose (fallback) — rose 更强烈, 提示用户应该重试
  const containerCls =
    tone === 'rose'
      ? 'bg-rose-900/30 border border-rose-700/50 text-rose-200'
      : 'bg-amber-900/30 border border-amber-700/50 text-amber-200';
  const buttonCls =
    tone === 'rose'
      ? 'bg-rose-700 hover:bg-rose-600 text-white focus-visible:ring-rose-400'
      : 'bg-amber-700 hover:bg-amber-600 text-white focus-visible:ring-amber-400';
  const dismissCls =
    tone === 'rose'
      ? 'text-rose-300 hover:text-rose-100 focus-visible:ring-rose-400'
      : 'text-amber-300 hover:text-amber-100 focus-visible:ring-amber-400';

  return (
    <div
      role="status"
      aria-live="polite"
      data-testid={`mock-banner-${source}`}
      className={`${containerCls} text-xs px-3 py-2 rounded-md flex items-start gap-2`}
    >
      <svg
        className="w-4 h-4 mt-0.5 flex-shrink-0"
        fill="none"
        stroke="currentColor"
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          strokeWidth={2}
          d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
        />
      </svg>
      <div className="flex-1 min-w-0">
        <div className="font-semibold">{title}</div>
        {/* sr-only 展开: 让屏幕阅读器读到完整解释, 不依赖 title 提示 */}
        <span className="sr-only">。{description}</span>
        <div className="hidden sm:block text-[11px] mt-0.5 opacity-80">{description}</div>
      </div>
      <div className="flex items-center gap-1 flex-shrink-0">
        {onRetry && (
          <button
            type="button"
            onClick={onRetry}
            className={`${buttonCls} px-2 py-1 rounded text-[11px] font-medium transition-colors focus:outline-none focus-visible:ring-2`}
            aria-label="重试获取真实数据"
          >
            重试
          </button>
        )}
        {onDismiss && (
          <button
            type="button"
            onClick={onDismiss}
            className={`${dismissCls} p-1 rounded focus:outline-none focus-visible:ring-2`}
            aria-label="关闭提示"
          >
            <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        )}
      </div>
    </div>
  );
}
