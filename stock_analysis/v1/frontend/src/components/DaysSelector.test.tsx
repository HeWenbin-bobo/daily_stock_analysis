import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { DaysSelector } from './DaysSelector';

describe('DaysSelector', () => {
  afterEach(() => {
    cleanup();
  });

  it('renders all 5 preset options', () => {
    const onChange = vi.fn();
    render(<DaysSelector value={365} onChange={onChange} />);

    expect(screen.getByText('3个月')).toBeInTheDocument();
    expect(screen.getByText('6个月')).toBeInTheDocument();
    expect(screen.getByText('1年')).toBeInTheDocument();
    expect(screen.getByText('2年')).toBeInTheDocument();
    expect(screen.getByText('5年')).toBeInTheDocument();
  });

  it('renders exactly 5 buttons', () => {
    const onChange = vi.fn();
    render(<DaysSelector value={365} onChange={onChange} />);

    const buttons = screen.getAllByRole('button');
    expect(buttons).toHaveLength(5);
  });

  it('marks the active value with aria-pressed', () => {
    const onChange = vi.fn();
    render(<DaysSelector value={730} onChange={onChange} />);

    const findBtn = (label: string) =>
      screen.getAllByRole('button').find((b) => b.textContent?.includes(label))!;

    expect(findBtn('3个月')).toHaveAttribute('aria-pressed', 'false');
    expect(findBtn('2年')).toHaveAttribute('aria-pressed', 'true');
    expect(findBtn('5年')).toHaveAttribute('aria-pressed', 'false');
  });

  it('calls onChange with numeric value when clicked', () => {
    const onChange = vi.fn();
    render(<DaysSelector value={365} onChange={onChange} />);

    const buttons = screen.getAllByRole('button');
    const fiveYearBtn = buttons.find((b) => b.textContent?.includes('5年'))!;
    fiveYearBtn.click();

    expect(onChange).toHaveBeenCalledWith(1825);
  });

  it('calls onChange for each preset', () => {
    const onChange = vi.fn();
    render(<DaysSelector value={365} onChange={onChange} />);

    const labels = ['3个月', '6个月', '1年', '2年', '5年'];
    const expectedDays = [90, 180, 365, 730, 1825];

    labels.forEach((label, i) => {
      const btn = screen.getAllByRole('button').find((b) => b.textContent?.includes(label))!;
      btn.click();
      expect(onChange).toHaveBeenLastCalledWith(expectedDays[i]);
    });
    expect(onChange).toHaveBeenCalledTimes(5);
  });

  it('updates aria-pressed when value changes', () => {
    const onChange = vi.fn();
    const { rerender } = render(<DaysSelector value={365} onChange={onChange} />);

    const findBtn = (label: string) =>
      screen.getAllByRole('button').find((b) => b.textContent?.includes(label))!;

    expect(findBtn('1年')).toHaveAttribute('aria-pressed', 'true');

    rerender(<DaysSelector value={90} onChange={onChange} />);

    expect(findBtn('1年')).toHaveAttribute('aria-pressed', 'false');
    expect(findBtn('3个月')).toHaveAttribute('aria-pressed', 'true');
  });

  it('each button has accessible title (回看 X)', () => {
    const onChange = vi.fn();
    render(<DaysSelector value={365} onChange={onChange} />);

    const buttons = screen.getAllByRole('button');
    buttons.forEach((btn) => {
      expect(btn).toHaveAttribute('title');
      expect(btn.getAttribute('title')).toMatch(/^回看/);
    });
  });
});
