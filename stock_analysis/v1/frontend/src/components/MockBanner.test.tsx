/// <reference types="vitest/globals" />
import '@testing-library/jest-dom/vitest';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { MockBanner } from './MockBanner';

describe('MockBanner', () => {
  it('renders nothing for real data sources', () => {
    const { container } = render(<MockBanner source="yfinance" />);
    expect(container.firstChild).toBeNull();

    render(<MockBanner source="akshare" />);
    expect(screen.queryByRole('status')).toBeNull();

    // iter 162: tencent 也是真实数据源 (A股/港股腾讯直连兜底), 不应显示 banner
    render(<MockBanner source="tencent" />);
    expect(screen.queryByRole('status')).toBeNull();

    render(<MockBanner source="finnhub" />);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('shows amber banner for source="mock" (user chose mock)', () => {
    render(<MockBanner source="mock" />);
    const banner = screen.getByRole('status');
    expect(banner).toBeInTheDocument();
    expect(banner).toHaveAttribute('data-testid', 'mock-banner-mock');
    expect(banner.textContent).toContain('演示数据');
  });

  it('shows rose banner for source="mock_fallback" (upstream failed)', () => {
    render(<MockBanner source="mock_fallback" />);
    const banner = screen.getByRole('status');
    expect(banner).toBeInTheDocument();
    expect(banner).toHaveAttribute('data-testid', 'mock-banner-mock_fallback');
    expect(banner.textContent).toContain('数据源暂时不可用');
  });

  it('calls onRetry when retry button clicked', () => {
    const onRetry = vi.fn();
    render(<MockBanner source="mock_fallback" onRetry={onRetry} />);
    fireEvent.click(screen.getByRole('button', { name: '重试获取真实数据' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('calls onDismiss when close button clicked', () => {
    const onDismiss = vi.fn();
    render(<MockBanner source="mock" onDismiss={onDismiss} />);
    fireEvent.click(screen.getByRole('button', { name: '关闭提示' }));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('hides retry/dismiss buttons when handlers not provided', () => {
    render(<MockBanner source="mock" />);
    expect(screen.queryByRole('button', { name: '重试获取真实数据' })).toBeNull();
    expect(screen.queryByRole('button', { name: '关闭提示' })).toBeNull();
  });

  it('exposes full description to screen readers via sr-only', () => {
    render(<MockBanner source="mock_fallback" />);
    // sr-only text: 让屏幕阅读器读到详细解释
    expect(
      screen.getByText(/akshare 或 yfinance 上游请求失败/, { selector: '.sr-only' })
    ).toBeInTheDocument();
  });
});
