import { describe, expect, it } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { KLineChartSkeleton, PeakTroughSkeleton } from './Skeletons';

describe('Skeletons', () => {
  describe('KLineChartSkeleton', () => {
    it('renders with animate-pulse class', () => {
      const { container } = render(<KLineChartSkeleton />);
      const pulseDiv = container.querySelector('.animate-pulse');
      expect(pulseDiv).toBeTruthy();
    });

    it('renders 40 bar placeholders', () => {
      const { container } = render(<KLineChartSkeleton />);
      // 40 根柱子的 div — iter 77: light/dark 双类
      const bars = container.querySelectorAll('.flex-1.bg-slate-300');
      expect(bars.length).toBe(40);
    });

    it('renders skeleton footer', () => {
      const { container } = render(<KLineChartSkeleton />);
      // 底部时间轴占位 — iter 77: light/dark 双类
      const footer = container.querySelector('.bg-slate-300.rounded.mt-3');
      expect(footer).toBeTruthy();
    });
  });

  describe('PeakTroughSkeleton', () => {
    it('renders with animate-pulse class', () => {
      const { container } = render(<PeakTroughSkeleton />);
      const pulseDiv = container.querySelector('.animate-pulse');
      expect(pulseDiv).toBeTruthy();
    });

    it('renders 5 summary card placeholders', () => {
      const { container } = render(<PeakTroughSkeleton />);
      // 5 个 summary card grid
      const grid = container.querySelector('.grid');
      expect(grid).toBeTruthy();
      // 5 个卡片
      const cards = grid?.querySelectorAll(':scope > div');
      expect(cards?.length).toBe(5);
    });

    it('renders table row placeholders', () => {
      const { container } = render(<PeakTroughSkeleton />);
      // 5 行表格 — iter 77: light/dark 双类
      const rows = container.querySelectorAll('.h-10.bg-slate-200');
      expect(rows.length).toBe(5);
    });
  });
});
