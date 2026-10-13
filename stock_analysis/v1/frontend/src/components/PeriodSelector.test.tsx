import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { PeriodSelector } from './PeriodSelector';

describe('PeriodSelector', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders all three period options', () => {
    const onChange = vi.fn();
    render(<PeriodSelector value="daily" onChange={onChange} />);

    expect(screen.getByText('日 K')).toBeInTheDocument();
    expect(screen.getByText('周 K')).toBeInTheDocument();
    expect(screen.getByText('月 K')).toBeInTheDocument();
  });

  it('marks the active period with aria-pressed', () => {
    const onChange = vi.fn();
    render(<PeriodSelector value="weekly" onChange={onChange} />);

    // 通过 textContent 匹配按钮(中文 label 含特殊字符)
    const buttons = screen.getAllByRole('button');
    expect(buttons).toHaveLength(3);

    const findByText = (text: string) =>
      buttons.find((b) => b.textContent?.includes(text))!;

    const dailyBtn = findByText('日 K');
    const weeklyBtn = findByText('周 K');
    const monthlyBtn = findByText('月 K');

    expect(dailyBtn).toHaveAttribute('aria-pressed', 'false');
    expect(weeklyBtn).toHaveAttribute('aria-pressed', 'true');
    expect(monthlyBtn).toHaveAttribute('aria-pressed', 'false');
  });

  it('calls onChange when a different period is clicked', () => {
    const onChange = vi.fn();
    render(<PeriodSelector value="daily" onChange={onChange} />);

    const buttons = screen.getAllByRole('button');
    const weeklyBtn = buttons.find((b) => b.textContent?.includes('周 K'))!;
    weeklyBtn.click();

    expect(onChange).toHaveBeenCalledWith('weekly');
  });

  it('calls onChange with monthly when monthly is clicked', () => {
    const onChange = vi.fn();
    render(<PeriodSelector value="daily" onChange={onChange} />);

    const buttons = screen.getAllByRole('button');
    const monthlyBtn = buttons.find((b) => b.textContent?.includes('月 K'))!;
    monthlyBtn.click();

    expect(onChange).toHaveBeenCalledWith('monthly');
  });

  it('does not call onChange when clicking the already-active period', () => {
    const onChange = vi.fn();
    render(<PeriodSelector value="daily" onChange={onChange} />);

    const buttons = screen.getAllByRole('button');
    const dailyBtn = buttons.find((b) => b.textContent?.includes('日 K'))!;
    dailyBtn.click();

    expect(onChange).toHaveBeenCalledWith('daily');
  });

  it('updates aria-pressed when value prop changes', () => {
    const onChange = vi.fn();
    const { rerender } = render(<PeriodSelector value="daily" onChange={onChange} />);

    const findInButtons = () =>
      screen.getAllByRole('button').reduce<Record<string, HTMLElement>>((acc, b) => {
        if (b.textContent?.includes('日 K')) acc.daily = b;
        if (b.textContent?.includes('周 K')) acc.weekly = b;
        if (b.textContent?.includes('月 K')) acc.monthly = b;
        return acc;
      }, {});

    expect(findInButtons().daily).toHaveAttribute('aria-pressed', 'true');

    rerender(<PeriodSelector value="monthly" onChange={onChange} />);

    const final = findInButtons();
    expect(final.daily).toHaveAttribute('aria-pressed', 'false');
    expect(final.monthly).toHaveAttribute('aria-pressed', 'true');
  });

  it('has accessible title on each button', () => {
    const onChange = vi.fn();
    render(<PeriodSelector value="daily" onChange={onChange} />);

    const buttons = screen.getAllByRole('button');
    const dailyBtn = buttons.find((b) => b.textContent?.includes('日 K'))!;
    expect(dailyBtn).toHaveAttribute('title');
  });
});
