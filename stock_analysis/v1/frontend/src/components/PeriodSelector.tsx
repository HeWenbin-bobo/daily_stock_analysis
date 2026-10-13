/**
 * K线周期选择器, 提供日/周/月切换按钮组。按钮使用 aria-pressed 反映选中状态,
 * type=button 防止表单意外提交 (iter 74), useRovingTabIndex 支持键盘导航 (iter 87)。
 */
import { memo } from 'react';
import type { Period } from '../types';
import { useRovingTabIndex } from '../hooks/useRovingTabIndex';

interface Props {
  value: Period;
  onChange: (p: Period) => void;
}

const OPTIONS: Array<{ value: Period; label: string }> = [
  { value: 'daily', label: '日 K' },
  { value: 'weekly', label: '周 K' },
  { value: 'monthly', label: '月 K' },
];

// iter 87: React.memo — 父组件 (App) 频繁 setState (selectedStock/period/days/loadingHistory
// 等), selector 只接收 value/onChange, 没有 props 变化就不该重渲染。
// 之前 watchlist 增减/AI loading 切换都会让 PeriodSelector 跟着重建, 实际无视觉变化。
export const PeriodSelector = memo(function PeriodSelector({ value, onChange }: Props) {
  const { getItemProps } = useRovingTabIndex(OPTIONS.length);

  return (
    <div
      className="inline-flex bg-panel border border-border rounded-lg p-0.5"
      role="group"
      aria-label="K 线周期"
    >
      {OPTIONS.map((opt, idx) => (
        <button
          key={opt.value}
          type="button"
          {...getItemProps(idx)}
          onClick={() => onChange(opt.value)}
          title={`切换到${opt.label}线`}
          aria-pressed={value === opt.value}
          aria-label={`${opt.label}${value === opt.value ? ' (当前)' : ''}`}
          className={`px-4 py-2 text-sm rounded-md transition-colors
                     // iter 74: py-1.5 → py-2 (≈40px) 满足 WCAG 2.5.8 / Apple HIG 触摸目标 ≥ 44pt
                     focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 ${
            value === opt.value
              ? 'bg-blue-600 text-white'
              : 'text-slate-400 hover:text-slate-100'
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
});