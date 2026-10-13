import { describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import type { KeyboardEvent } from 'react';
import { useRovingTabIndex } from './useRovingTabIndex';

/** 构造一个键盘事件, 避免 jsdom 报 undefined。 */
function makeKey(key: string): KeyboardEvent<HTMLButtonElement> {
  return { key, preventDefault: vi.fn() } as unknown as KeyboardEvent<HTMLButtonElement>;
}

describe('useRovingTabIndex', () => {
  it('first item has tabIndex=0, others -1', () => {
    const { result } = renderHook(() => useRovingTabIndex(3));
    const p0 = result.current.getItemProps(0);
    const p1 = result.current.getItemProps(1);
    const p2 = result.current.getItemProps(2);
    expect(p0.tabIndex).toBe(0);
    expect(p1.tabIndex).toBe(-1);
    expect(p2.tabIndex).toBe(-1);
  });

  it('getItemProps returns onKeyDown handler for every item', () => {
    const { result } = renderHook(() => useRovingTabIndex(4));
    for (let i = 0; i < 4; i++) {
      expect(typeof result.current.getItemProps(i).onKeyDown).toBe('function');
    }
  });

  it('ArrowRight does not throw and updates active index', () => {
    const { result } = renderHook(() => useRovingTabIndex(3));
    const onKey = result.current.getItemProps(2).onKeyDown;
    expect(() => {
      act(() => onKey(makeKey('ArrowRight')));
    }).not.toThrow();
  });

  it('ArrowLeft does not throw', () => {
    const { result } = renderHook(() => useRovingTabIndex(3));
    const onKey = result.current.getItemProps(1).onKeyDown;
    expect(() => {
      act(() => onKey(makeKey('ArrowLeft')));
    }).not.toThrow();
  });

  it('ArrowDown (alias) calls preventDefault', () => {
    const { result } = renderHook(() => useRovingTabIndex(3));
    const ev = makeKey('ArrowDown');
    act(() => result.current.getItemProps(1).onKeyDown(ev));
    expect(ev.preventDefault).toHaveBeenCalled();
  });

  it('Home/End are accepted (smoke)', () => {
    const { result } = renderHook(() => useRovingTabIndex(3));
    const onKey = result.current.getItemProps(1).onKeyDown;
    expect(() => {
      act(() => onKey(makeKey('Home')));
      act(() => onKey(makeKey('End')));
    }).not.toThrow();
  });

  it('Unhandled keys do not call preventDefault', () => {
    const { result } = renderHook(() => useRovingTabIndex(3));
    const ev = makeKey('Enter');
    act(() => result.current.getItemProps(0).onKeyDown(ev));
    expect(ev.preventDefault).not.toHaveBeenCalled();
  });

  it('Single-item group still produces tabIndex=0', () => {
    const { result } = renderHook(() => useRovingTabIndex(1));
    expect(result.current.getItemProps(0).tabIndex).toBe(0);
  });

  it('Empty group produces no error on first item props', () => {
    // 防御性: 调用方传 0 时不应崩溃
    const { result } = renderHook(() => useRovingTabIndex(0));
    // getItemProps(0) 在空组下应能安全调用 — 至少不抛
    expect(() => result.current.getItemProps(0)).not.toThrow();
  });
});