/**
 * Lightweight-charts theme palettes (iter 82).
 *
 * Shared between KLineChart and IndicatorChart — both previously duplicated these constants.
 * Theme detection reads `html.light` class via isDarkTheme() at chart init and on every
 * MutationObserver 'class' attribute change (real-time theme switching).
 */
export const DARK_BG = '#0f172a';
export const LIGHT_BG = '#ffffff';
export const DARK_TEXT = '#cbd5e1';
export const LIGHT_TEXT = '#475569';
export const DARK_GRID = '#1e293b';
export const LIGHT_GRID = '#e2e8f0';
export const DARK_BORDER = '#334155';
export const LIGHT_BORDER = '#cbd5e1';

export function isDarkTheme(): boolean {
  if (typeof document === 'undefined') return true;
  return !document.documentElement.classList.contains('light');
}

/** Apply theme-aware colors to a lightweight-charts IChartApi instance. */
export function applyChartTheme(chart: import('lightweight-charts').IChartApi | null): void {
  if (!chart) return;
  const dark = isDarkTheme();
  chart.applyOptions({
    layout: {
      background: { color: dark ? DARK_BG : LIGHT_BG },
      textColor: dark ? DARK_TEXT : LIGHT_TEXT,
    },
    grid: {
      vertLines: { color: dark ? DARK_GRID : LIGHT_GRID },
      horzLines: { color: dark ? DARK_GRID : LIGHT_GRID },
    },
    timeScale: { borderColor: dark ? DARK_BORDER : LIGHT_BORDER },
    rightPriceScale: { borderColor: dark ? DARK_BORDER : LIGHT_BORDER },
  });
}