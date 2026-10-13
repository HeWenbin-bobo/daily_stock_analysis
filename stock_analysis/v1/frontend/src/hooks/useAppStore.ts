import { create } from 'zustand';
import type { Period, StockSearchItem } from '../types';

interface AppState {
  selectedStock: StockSearchItem | null;
  period: Period;
  days: number;
  setSelectedStock: (s: StockSearchItem | null) => void;
  setPeriod: (p: Period) => void;
  setDays: (d: number) => void;
}

export const useAppStore = create<AppState>((set) => ({
  selectedStock: null,
  period: 'daily',
  days: 365,
  setSelectedStock: (s) => set({ selectedStock: s }),
  setPeriod: (p) => set({ period: p }),
  setDays: (d) => set({ days: d }),
}));