import { useCallback, useEffect, useState } from 'react';

type Theme = 'dark' | 'light';

const STORAGE_KEY = 'theme';

/** 安全读取 localStorage (隐私模式 / SecurityError 下不崩) */
function safeGetStoredTheme(): string | null {
  try {
    if (typeof window === 'undefined') return null;
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

/** 安全写入 localStorage (隐私模式 / 配额超出时降级忽略) */
function safeSetStoredTheme(theme: Theme): void {
  try {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    // 隐私模式 / 配额超出 -> 静默忽略, 主题仍可在当前会话生效
  }
}

function getInitialTheme(): Theme {
  // iter 76: 永远先查 localStorage (即使在 SSR context 也能从注入的脚本读),
  // 避免客户端 hydration 时先用默认 'dark' 渲染, 再 useEffect 切到 'light' 的 FOUC
  const stored = safeGetStoredTheme();
  if (stored === 'light' || stored === 'dark') return stored;
  if (typeof window !== 'undefined' && window.matchMedia?.('(prefers-color-scheme: light)').matches) {
    return 'light';
  }
  return 'dark';
}

function applyTheme(theme: Theme): void {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  root.classList.remove('light', 'dark');
  root.classList.add(theme);
  root.style.colorScheme = theme;
}

export function useTheme(): { theme: Theme; toggle: () => void; setTheme: (t: Theme) => void } {
  const [theme, setThemeState] = useState<Theme>(getInitialTheme);

  useEffect(() => {
    applyTheme(theme);
    safeSetStoredTheme(theme);
  }, [theme]);

  // iter 69: 用 useCallback 稳定 setTheme / toggle 引用, 与 React.memo 子组件契约一致
  const setTheme = useCallback((t: Theme) => setThemeState(t), []);
  const toggle = useCallback(
    () => setThemeState((t) => (t === 'dark' ? 'light' : 'dark')),
    []
  );

  return { theme, setTheme, toggle };
}