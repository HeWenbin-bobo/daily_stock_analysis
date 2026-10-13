/** K线图加载骨架屏 */
import { useMemo } from 'react';
export function KLineChartSkeleton() {
  // 预计算骨架条高度, 避免每次 render 都重新生成导致抖动
  // (Math.random() 在 render 中调用会在重渲染时让高度跳变, 破坏脉冲动画)
  const heights = useMemo(
    () => Array.from({ length: 40 }, () => 30 + Math.random() * 60),
    [],
  );
  return (
    <div className="animate-pulse h-full flex flex-col">
      {/* 模拟顶部图例 */}
      <div className="h-6 bg-slate-300 dark:bg-slate-700/40 rounded mb-3 w-1/3"></div>
      {/* 模拟 K 线主体 */}
      <div className="flex-1 flex items-end gap-1 px-4">
        {heights.map((h, i) => (
          <div
            key={i}
            className="flex-1 bg-slate-300 dark:bg-slate-700/40 rounded-t"
            style={{ height: `${h}%` }}
          />
        ))}
      </div>
      {/* 模拟底部时间轴 */}
      <div className="h-4 bg-slate-300 dark:bg-slate-700/40 rounded mt-3 w-full"></div>
    </div>
  );
}

/** 波峰波谷面板加载骨架屏 */
export function PeakTroughSkeleton() {
  return (
    <div className="bg-panel border border-border rounded-lg p-4 space-y-4 animate-pulse">
      {/* 摘要卡片 */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
        {Array.from({ length: 5 }).map((_, i) => (
          <div key={i} className="bg-slate-200 dark:bg-slate-800/40 border border-border/50 rounded-lg p-3">
            <div className="h-3 bg-slate-300 dark:bg-slate-700/40 rounded w-1/2 mb-2"></div>
            <div className="h-5 bg-slate-300 dark:bg-slate-700/40 rounded w-3/4"></div>
          </div>
        ))}
      </div>
      {/* 表格 */}
      <div>
        <div className="h-4 bg-slate-300 dark:bg-slate-700/40 rounded w-1/4 mb-2"></div>
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <div key={i} className="h-10 bg-slate-200 dark:bg-slate-800/30 rounded"></div>
          ))}
        </div>
      </div>
    </div>
  );
}
