import { useCallback, useEffect, useRef, type KeyboardEvent } from 'react';

interface UseRovingTabIndexReturn {
  /** Spreads onto each button (tabIndex, ref, onKeyDown). */
  getItemProps: (index: number) => {
    ref: (el: HTMLButtonElement | null) => void;
    tabIndex: 0 | -1;
    onKeyDown: (e: KeyboardEvent<HTMLButtonElement>) => void;
  };
}

/**
 * Roving-tabindex keyboard navigation for a group of buttons.
 * Implements WAI-ARIA Authoring Practices "Toolbar" pattern:
 *   - ArrowLeft/ArrowRight (or ArrowUp/ArrowDown) move focus between items
 *   - Home/End jump to first/last
 *   - Wraps around at the ends
 *
 * Returns:
 *   - getItemProps(index): spreads onto each button (tabIndex, ref, onKeyDown, onFocus)
 *   - containerProps: spreads onto the group container (onKeyDown aggregator is handled per-item)
 *
 * Only the active item has tabIndex=0; the rest are tabIndex=-1 so Tab skips the group,
 * but arrow keys still move within it.
 */
export function useRovingTabIndex(count: number): UseRovingTabIndexReturn {
  const refs = useRef<Array<HTMLButtonElement | null>>([]);

  const focusIndex = useCallback((idx: number) => {
    // iter 73: count=0 守卫 — (idx + 0) % 0 = NaN, refs.current[NaN] = undefined, .focus() 抛 TypeError
    if (count <= 0) return;
    const wrapped = (idx + count) % count;
    const target = refs.current[wrapped];
    target?.focus();
    // iter 92: 滚动到视口 — 当工具栏很多项目或视口较小时, 箭头键导航可能让焦点
    // 跑到屏幕外, 用户失去视觉反馈。block:'nearest' 只在必要时滚动, 不破坏默认行为。
    target?.scrollIntoView({ block: "nearest" });
  }, [count]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLButtonElement>, idx: number) => {
      switch (e.key) {
        case 'ArrowLeft':
        case 'ArrowUp':
          e.preventDefault();
          focusIndex(idx - 1);
          break;
        case 'ArrowRight':
        case 'ArrowDown':
          e.preventDefault();
          focusIndex(idx + 1);
          break;
        case 'Home':
          e.preventDefault();
          focusIndex(0);
          break;
        case 'End':
          e.preventDefault();
          focusIndex(count - 1);
          break;
      }
    },
    [focusIndex, count],
  );

  // iter 79: 把 handleKeyDown 存到 ref, 让 getItemProps 每次返回的 onKeyDown 引用稳定。
  // 之前每次 render 都新建箭头函数, 让 memo 包了 onKeyDown prop 的 button 子组件无法做
  // reference equality 比较, 导致不必要重渲染。
  const handleKeyDownRef = useRef(handleKeyDown);
  handleKeyDownRef.current = handleKeyDown;

  // iter 85: 在 ref callback 里加 unmount cleanup — 组件卸载时把 refs.current[index]
  // 置 null, 避免 count 缩小后旧 slot 残留 stale DOM element。
  // cleanup 在 index 不再被新组件使用时触发, 让 refs.current 数组自然收缩。
  const getItemProps = useCallback(
    (index: number) => ({
      ref: (el: HTMLButtonElement | null) => {
        refs.current[index] = el;
      },
      // iter 133: 显式断言 tabIndex: 0 | -1 — 三元表达式默认推断为 number,
      // 与接口签名 UseRovingTabIndexReturn 不兼容 (tsc --strict)。as const 不能
      // 加在括号表达式上, 用类型断言 as 0 | -1。
      tabIndex: (index === 0 ? 0 : -1) as 0 | -1,
      onKeyDown: (e: KeyboardEvent<HTMLButtonElement>) => handleKeyDownRef.current(e, index),
    }),
    [],
  );

  // 在 useEffect 里观察 count, 当 count 缩短时截断 refs.current 数组
  // (避免历史 items 数大于当前 count 时 refs.current 保留旧 DOM 引用)。
  // iter 94: 加 [count] deps + prevCountRef — 之前空 deps 让 effect 在每次 render 跑,
  // 读 refs.current.length 时可能拿到未提交的 stale 长度。prevCountRef 保证只在
  // count 实际缩小时截断, 首次 render 跳过 (length 默认 === count)。
  const prevCountRef = useRef(count);
  useEffect(() => {
    if (count < prevCountRef.current) {
      refs.current.length = count;
    }
    prevCountRef.current = count;
  }, [count]);

  return { getItemProps };
}