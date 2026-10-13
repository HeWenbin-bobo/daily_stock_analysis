import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { Watchlist } from './Watchlist';
import type { StockSearchItem } from '../types';

const sampleItems: StockSearchItem[] = [
  { code: '600519', name: '贵州茅台', market: 'cn' },
  { code: '00700', name: '腾讯控股', market: 'hk' },
  { code: 'AAPL', name: 'Apple Inc.', market: 'us' },
];

describe('Watchlist', () => {
  afterEach(() => {
    cleanup();
  });

  describe('basic rendering', () => {
    it('shows a helpful empty-state card when items is empty', () => {
      render(
        <Watchlist items={[]} onSelect={() => {}} onRemove={() => {}} />
      );
      // 空状态: 提示用户如何添加自选 (而非渲染 null)
      expect(screen.getByText('自选股为空')).toBeInTheDocument();
      expect(
        screen.getByText(/在上方搜索股票/),
      ).toBeInTheDocument();
    });

    it('renders all items with name and code', () => {
      render(
        <Watchlist items={sampleItems} onSelect={() => {}} onRemove={() => {}} />
      );
      expect(screen.getByText('贵州茅台')).toBeInTheDocument();
      expect(screen.getByText('腾讯控股')).toBeInTheDocument();
      expect(screen.getByText('Apple Inc.')).toBeInTheDocument();
      // code 显示
      const codes = screen.getAllByText(/600519|00700|AAPL/);
      expect(codes.length).toBeGreaterThanOrEqual(3);
    });

    it('shows count in header', () => {
      render(
        <Watchlist items={sampleItems} onSelect={() => {}} onRemove={() => {}} />
      );
      expect(screen.getByText(/我的自选 \(3\)/)).toBeInTheDocument();
    });

    it('shows market label badge for each item', () => {
      render(
        <Watchlist items={sampleItems} onSelect={() => {}} onRemove={() => {}} />
      );
      expect(screen.getByText('A股')).toBeInTheDocument();
      expect(screen.getByText('港股')).toBeInTheDocument();
      expect(screen.getByText('美股')).toBeInTheDocument();
    });
  });

  describe('interactions', () => {
    it('calls onSelect when item is clicked', () => {
      const onSelect = vi.fn();
      render(
        <Watchlist items={sampleItems} onSelect={onSelect} onRemove={() => {}} />
      );
      const maotaiBtn = screen.getByText('贵州茅台').closest('button')!;
      maotaiBtn.click();
      expect(onSelect).toHaveBeenCalledWith(sampleItems[0]);
    });

    it('calls onRemove when remove button is clicked', () => {
      const onRemove = vi.fn();
      render(
        <Watchlist items={sampleItems} onSelect={() => {}} onRemove={onRemove} />
      );
      // 找第一个移除按钮(aria-label="移除 贵州茅台")
      const removeBtns = screen.getAllByLabelText(/移除/);
      expect(removeBtns.length).toBe(3);
      removeBtns[0].click();
      expect(onRemove).toHaveBeenCalledWith('600519', 'cn');
    });

    it('highlights currently selected stock', () => {
      render(
        <Watchlist
          items={sampleItems}
          onSelect={() => {}}
          onRemove={() => {}}
          currentCode="00700"
          currentMarket="hk"
        />
      );
      // 腾讯的容器应该有蓝色背景(bg-blue-900/30)
      const tengxunText = screen.getByText('腾讯控股');
      // closest('div') 返回 item 的最外层 div(含 bg-blue-900/30)
      const itemContainer = tengxunText.closest('div.flex');
      expect(itemContainer?.className).toContain('bg-blue-900');
    });
  });

  describe('import/export buttons', () => {
    it('does not show import button when onImport not provided', () => {
      render(
        <Watchlist items={sampleItems} onSelect={() => {}} onRemove={() => {}} />
      );
      expect(screen.queryByLabelText('导入自选股')).not.toBeInTheDocument();
    });

    it('shows import + export buttons when onImport provided', () => {
      render(
        <Watchlist
          items={sampleItems}
          onSelect={() => {}}
          onRemove={() => {}}
          onImport={() => {}}
        />
      );
      expect(screen.getByLabelText('导入自选股')).toBeInTheDocument();
      // iter 101: aria-label 改为动态 "导出自选股 (N 支) 为 JSON" — substring 匹配
      expect(screen.getByLabelText(/导出自选股.*为 JSON/)).toBeInTheDocument();
    });
  });

  describe('import status accessibility', () => {
    it('shows dismissible status region with role=status for success', async () => {
      const onImport = vi.fn();
      // 模拟 importWatchlistFile 成功
      const file = new File(['[]'], 'watchlist.json', { type: 'application/json' });
      const utils = await import('../utils/watchlistIO');
      vi.spyOn(utils, 'importWatchlistFile').mockResolvedValue(sampleItems);

      render(
        <Watchlist
          items={sampleItems}
          onSelect={() => {}}
          onRemove={() => {}}
          onImport={onImport}
        />
      );
      const input = document.querySelector('input[type=file]') as HTMLInputElement;
      // 直接派发 change 事件
      Object.defineProperty(input, 'files', { value: [file], configurable: true });
      input.dispatchEvent(new Event('change', { bubbles: true }));

      // 等异步 promise resolve
      const status = await screen.findByRole('status');
      expect(status).toHaveTextContent(/成功导入 3 支股票/);
      expect(status.getAttribute('aria-live')).toBe('polite');
    });

    it('shows role=alert for import failure', async () => {
      const utils = await import('../utils/watchlistIO');
      vi.spyOn(utils, 'importWatchlistFile').mockRejectedValue(new Error('文件太大'));

      const file = new File([''], 'bad.json', { type: 'application/json' });
      render(
        <Watchlist
          items={sampleItems}
          onSelect={() => {}}
          onRemove={() => {}}
          onImport={() => {}}
        />
      );
      const input = document.querySelector('input[type=file]') as HTMLInputElement;
      Object.defineProperty(input, 'files', { value: [file], configurable: true });
      input.dispatchEvent(new Event('change', { bubbles: true }));

      const alert = await screen.findByRole('alert');
      // iter 97: 错误信息不再泄露实现细节 (err.message 中的 JSON 结构提示),
      // 统一文案为 "文件格式不符合要求"
      expect(alert).toHaveTextContent(/文件格式不符合要求/);
      expect(alert.getAttribute('aria-live')).toBe('assertive');
    });
  });
});
