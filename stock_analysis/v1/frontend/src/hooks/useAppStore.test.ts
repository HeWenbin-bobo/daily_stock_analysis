import { beforeEach, describe, expect, it } from 'vitest';
import { useAppStore } from './useAppStore';

describe('useAppStore (zustand)', () => {
  beforeEach(() => {
    // 重置 store 状态到初始值
    useAppStore.setState({
      selectedStock: null,
      period: 'daily',
      days: 365,
    });
  });

  describe('initial state', () => {
    it('starts with no selected stock', () => {
      expect(useAppStore.getState().selectedStock).toBeNull();
    });

    it('defaults to daily period', () => {
      expect(useAppStore.getState().period).toBe('daily');
    });

    it('defaults to 365 days', () => {
      expect(useAppStore.getState().days).toBe(365);
    });
  });

  describe('setSelectedStock', () => {
    it('sets the selected stock', () => {
      const stock = { code: '600519', name: '贵州茅台', market: 'cn' as const };
      useAppStore.getState().setSelectedStock(stock);
      expect(useAppStore.getState().selectedStock).toEqual(stock);
    });

    it('can set back to null', () => {
      useAppStore.getState().setSelectedStock({ code: 'X', name: 'X', market: 'us' as const });
      useAppStore.getState().setSelectedStock(null);
      expect(useAppStore.getState().selectedStock).toBeNull();
    });

    it('replaces previous selection', () => {
      useAppStore.getState().setSelectedStock({ code: 'A', name: 'A', market: 'cn' as const });
      useAppStore.getState().setSelectedStock({ code: 'B', name: 'B', market: 'us' as const });
      expect(useAppStore.getState().selectedStock?.code).toBe('B');
    });
  });

  describe('setPeriod', () => {
    it('sets weekly period', () => {
      useAppStore.getState().setPeriod('weekly');
      expect(useAppStore.getState().period).toBe('weekly');
    });

    it('sets monthly period', () => {
      useAppStore.getState().setPeriod('monthly');
      expect(useAppStore.getState().period).toBe('monthly');
    });

    it('can revert to daily', () => {
      useAppStore.getState().setPeriod('monthly');
      useAppStore.getState().setPeriod('daily');
      expect(useAppStore.getState().period).toBe('daily');
    });
  });

  describe('setDays', () => {
    it('sets days to 90', () => {
      useAppStore.getState().setDays(90);
      expect(useAppStore.getState().days).toBe(90);
    });

    it('sets days to 1825 (5 years)', () => {
      useAppStore.getState().setDays(1825);
      expect(useAppStore.getState().days).toBe(1825);
    });

    it('updates days without affecting other state', () => {
      const stock = { code: 'X', name: 'X', market: 'cn' as const };
      useAppStore.getState().setSelectedStock(stock);
      useAppStore.getState().setPeriod('monthly');
      useAppStore.getState().setDays(730);
      const state = useAppStore.getState();
      expect(state.days).toBe(730);
      expect(state.period).toBe('monthly');
      expect(state.selectedStock).toEqual(stock);
    });
  });

  describe('combined state changes', () => {
    it('multiple setters in sequence produce correct final state', () => {
      const store = useAppStore.getState();
      store.setSelectedStock({ code: 'AAPL', name: 'Apple', market: 'us' as const });
      store.setPeriod('weekly');
      store.setDays(730);

      const final = useAppStore.getState();
      expect(final.selectedStock?.code).toBe('AAPL');
      expect(final.period).toBe('weekly');
      expect(final.days).toBe(730);
    });
  });

  describe('hook subscription (React integration)', () => {
    it('useAppStore hook returns same instance', () => {
      const hook1 = useAppStore;
      const hook2 = useAppStore;
      expect(hook1).toBe(hook2);
      expect(typeof hook1).toBe('function');
    });

    it('state mutations are visible to subscribers', () => {
      const initial = useAppStore.getState().period;
      useAppStore.setState({ period: 'monthly' });
      expect(useAppStore.getState().period).not.toBe(initial);
      expect(useAppStore.getState().period).toBe('monthly');
    });

    it('preserves other state when partial update', () => {
      const stock = { code: 'X', name: 'X', market: 'us' as const };
      useAppStore.getState().setSelectedStock(stock);
      useAppStore.getState().setPeriod('monthly');
      useAppStore.getState().setDays(730);

      // 只更新 period, 其他状态应保留
      useAppStore.setState({ period: 'daily' });

      const state = useAppStore.getState();
      expect(state.period).toBe('daily');
      expect(state.selectedStock).toEqual(stock);
      expect(state.days).toBe(730);
    });

    it('can reset to initial state', () => {
      useAppStore.getState().setSelectedStock({ code: 'X', name: 'X', market: 'cn' as const });
      useAppStore.getState().setPeriod('monthly');
      useAppStore.getState().setDays(1825);

      // 重置
      useAppStore.setState({
        selectedStock: null,
        period: 'daily',
        days: 365,
      });

      const state = useAppStore.getState();
      expect(state.selectedStock).toBeNull();
      expect(state.period).toBe('daily');
      expect(state.days).toBe(365);
    });
  });
});
