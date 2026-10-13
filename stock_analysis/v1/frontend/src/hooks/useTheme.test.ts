import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, renderHook } from '@testing-library/react';
import { useTheme } from './useTheme';

describe('useTheme', () => {
  beforeEach(() => {
    localStorage.clear();
    // Reset document classes
    document.documentElement.classList.remove('light', 'dark');
  });

  afterEach(() => {
    localStorage.clear();
    document.documentElement.classList.remove('light', 'dark');
  });

  it('defaults to dark when no localStorage and no system preference', () => {
    // Mock matchMedia to return no-preference
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
    });
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe('dark');
  });

  it('reads from localStorage', () => {
    localStorage.setItem('theme', 'light');
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe('light');
  });

  it('applies theme class to document', () => {
    const { result } = renderHook(() => useTheme());
    expect(document.documentElement.classList.contains('dark')).toBe(true);

    act(() => result.current.toggle());
    expect(document.documentElement.classList.contains('light')).toBe(true);
    expect(document.documentElement.classList.contains('dark')).toBe(false);
  });

  it('toggles between dark and light', () => {
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
    });
    const { result } = renderHook(() => useTheme());
    expect(result.current.theme).toBe('dark');

    act(() => result.current.toggle());
    expect(result.current.theme).toBe('light');

    act(() => result.current.toggle());
    expect(result.current.theme).toBe('dark');
  });

  it('persists theme to localStorage', () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current.toggle());
    expect(localStorage.getItem('theme')).toBe('light');
    act(() => result.current.toggle());
    expect(localStorage.getItem('theme')).toBe('dark');
  });

  it('setTheme directly updates state', () => {
    const { result } = renderHook(() => useTheme());
    act(() => result.current.setTheme('light'));
    expect(result.current.theme).toBe('light');
    expect(localStorage.getItem('theme')).toBe('light');
  });

  it('falls back to dark when localStorage.getItem throws (private mode)', () => {
    const original = Storage.prototype.getItem;
    Storage.prototype.getItem = vi.fn(() => {
      throw new Error('SecurityError: blocked');
    }) as unknown as typeof Storage.prototype.getItem;

    try {
      Object.defineProperty(window, 'matchMedia', {
        writable: true,
        value: () => ({ matches: false, addEventListener: () => {}, removeEventListener: () => {} }),
      });
      const { result } = renderHook(() => useTheme());
      expect(result.current.theme).toBe('dark');
    } finally {
      Storage.prototype.getItem = original;
    }
  });

  it('does not throw when localStorage.setItem throws (private mode)', () => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = vi.fn(() => {
      throw new Error('QuotaExceededError');
    }) as unknown as typeof Storage.prototype.setItem;

    try {
      const { result } = renderHook(() => useTheme());
      expect(() => {
        act(() => result.current.toggle());
      }).not.toThrow();
      expect(result.current.theme).toBe('light');
    } finally {
      Storage.prototype.setItem = original;
    }
  });
});
