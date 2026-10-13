/**
 * 回看天数选择器, 提供 90/180/365/730/1825 天选项。
 * React.memo 避免父组件重渲染时白白渲染 (iter 87), role=group 标识按钮组 (iter 92),
 * 同样使用 useRovingTabIndex 支持键盘导航。
 */
import { memo } from 'react';
import { useRovingTabIndex } from '../hooks/useRovingTabIndex';

interface Props {
  value: number;
  onChange: (days: number) => void;
}

const OPTIONS: Array<{ value: number; label: string }> = [
  { value: 90, label: '3个月' },
  { value: 180, label: '6个月' },
  { value: 365, label: '1年' },
  { value: 730, label: '2年' },
  { value: 1825, label: '5年' },
];

// iter 87: React.memo — 同 PeriodSelector 理由, 父组件重渲染时 selector 不必跟随。
export const DaysSelector = memo(function DaysSelector({ value, onChange }: Props) {
  const { getItemProps } = useRovingTabIndex(OPTIONS.length);

  return (
    <div
      className="inline-flex bg-panel border border-border rounded-lg p-0.5"
      role="group"
      aria-label="回看时长"
    >
      {OPTIONS.map((opt, idx) => (
        <button
          key={opt.value}
          type="button"
          {...getItemProps(idx)}
          onClick={() => onChange(opt.value)}
          title={`回看 ${opt.label}`}
          aria-pressed={value === opt.value}
          aria-label={`回看 ${opt.label}${value === opt.value ? ' (当前)' : ''}`}
          className={`px-3 py-2 text-xs rounded-md transition-colors
                     // iter 74: py-1.5 → py-2 提升到 ~36px 高度, 满足 WCAG 2.5.8 触摸目标最小尺寸
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