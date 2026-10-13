import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';

// Mock the API client module BEFORE importing the component
const mockSearchStocks = vi.fn();
vi.mock('../api/client', () => ({
  searchStocks: (...args: unknown[]) => mockSearchStocks(...args),
  isCancelError: () => false,
}));

// Import after mock setup
import { SearchBar } from './SearchBar';

describe('SearchBar', () => {
  beforeEach(() => {
    mockSearchStocks.mockReset();
  });

  afterEach(() => {
    cleanup();
  });

  it('renders input with placeholder', () => {
    render(<SearchBar onSelect={() => {}} />);
    const input = screen.getByPlaceholderText(/搜索股票/);
    expect(input).toBeInTheDocument();
  });

  it('does not call searchStocks on empty input', async () => {
    render(<SearchBar onSelect={() => {}} />);
    const input = screen.getByPlaceholderText(/搜索股票/);
    // 输入一个空格
    (input as HTMLInputElement).value = '   ';
    input.dispatchEvent(new Event('change', { bubbles: true }));
    // 等到 debounce 触发
    await waitFor(() => {}, { timeout: 500 });
    expect(mockSearchStocks).not.toHaveBeenCalled();
  });

  it('debounces input by 300ms', async () => {
    mockSearchStocks.mockResolvedValue([]);
    render(<SearchBar onSelect={() => {}} />);
    const input = screen.getByPlaceholderText(/搜索股票/) as HTMLInputElement;

    // 使用 fireEvent 触发 React 状态更新
    fireEvent.change(input, { target: { value: 'A' } });
    fireEvent.change(input, { target: { value: 'AA' } });
    fireEvent.change(input, { target: { value: 'AAPL' } });

    // 立即检查 - 还未触发
    expect(mockSearchStocks).not.toHaveBeenCalled();

    // 等待 debounce + 调用
    await waitFor(
      () => {
        expect(mockSearchStocks).toHaveBeenCalled();
      },
      { timeout: 1000 }
    );

    // 只应调用一次(防抖生效)
    expect(mockSearchStocks).toHaveBeenCalledTimes(1);
    expect(mockSearchStocks).toHaveBeenCalledWith('AAPL', expect.any(AbortSignal));
  });

  it('displays search results when API returns items', async () => {
    mockSearchStocks.mockResolvedValue([
      { code: 'AAPL', name: 'Apple', market: 'us' as const },
    ]);
    render(<SearchBar onSelect={() => {}} />);
    const input = screen.getByPlaceholderText(/搜索股票/) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'AAPL' } });

    await waitFor(() => {
      expect(screen.getByText('Apple')).toBeInTheDocument();
    });
  });

  it('displays "no match" when API returns empty', async () => {
    mockSearchStocks.mockResolvedValue([]);
    render(<SearchBar onSelect={() => {}} />);
    const input = screen.getByPlaceholderText(/搜索股票/) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'xyz' } });

    await waitFor(() => {
      expect(screen.getByText(/未找到匹配/)).toBeInTheDocument();
    });
  });

  it('passes AbortSignal to searchStocks', async () => {
    mockSearchStocks.mockResolvedValue([]);
    render(<SearchBar onSelect={() => {}} />);
    const input = screen.getByPlaceholderText(/搜索股票/) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'AAPL' } });

    await waitFor(() => {
      expect(mockSearchStocks).toHaveBeenCalled();
    });
    const callArgs = mockSearchStocks.mock.calls[0];
    // 第二参数应是 AbortSignal
    expect(callArgs[1]).toBeInstanceOf(AbortSignal);
  });

  it('resets loading and closes listbox when input becomes empty', async () => {
    // 慢响应以保持 loading=true 期间被清空
    let resolveSearch: (v: unknown) => void = () => {};
    mockSearchStocks.mockImplementation(
      () => new Promise((res) => { resolveSearch = res; })
    );

    render(<SearchBar onSelect={() => {}} />);
    const input = screen.getByPlaceholderText(/搜索股票/) as HTMLInputElement;

    fireEvent.change(input, { target: { value: 'AAPL' } });
    // 等 debounce 触发
    await waitFor(() => {
      expect(mockSearchStocks).toHaveBeenCalled();
    });
    // 此时 loading 应为 true
    expect(screen.getByText(/搜索中.../)).toBeInTheDocument();

    // 清空输入 - loading 必须重置, 面板关闭
    fireEvent.change(input, { target: { value: '' } });

    await waitFor(() => {
      expect(screen.queryByText(/搜索中.../)).not.toBeInTheDocument();
    });
    // 解决之前未完成的请求, 避免泄漏
    resolveSearch([]);
  });

  it('exposes ARIA combobox semantics on the input', () => {
    render(<SearchBar onSelect={() => {}} />);
    const input = screen.getByPlaceholderText(/搜索股票/) as HTMLInputElement;
    expect(input.getAttribute('role')).toBe('combobox');
    expect(input.getAttribute('aria-autocomplete')).toBe('list');
    expect(input.getAttribute('aria-label')).toMatch(/搜索/);
  });

  it('exposes listbox role on results and option role on items', async () => {
    mockSearchStocks.mockResolvedValue([
      { code: 'AAPL', name: 'Apple', market: 'us' as const },
      { code: 'MSFT', name: 'Microsoft', market: 'us' as const },
    ]);
    render(<SearchBar onSelect={() => {}} />);
    const input = screen.getByPlaceholderText(/搜索股票/) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'AA' } });

    await waitFor(() => {
      expect(screen.getByRole('listbox')).toBeInTheDocument();
    });
    expect(screen.getAllByRole('option')).toHaveLength(2);
  });

  it('does not refire search with selected label after option click', async () => {
    // B-3 修复回归: 选中后将输入设为 "Name (Code)" 不应触发新一轮搜索
    mockSearchStocks.mockResolvedValue([
      { code: 'AAPL', name: 'Apple Inc.', market: 'us' as const },
    ]);
    const onSelect = vi.fn();
    render(<SearchBar onSelect={onSelect} />);
    const input = screen.getByPlaceholderText(/搜索股票/) as HTMLInputElement;
    fireEvent.change(input, { target: { value: 'AA' } });

    await waitFor(() => {
      expect(screen.getByRole('listbox')).toBeInTheDocument();
    });

    mockSearchStocks.mockClear();

    const option = screen.getByRole('option');
    fireEvent.click(option);

    // iter 84: 用 waitFor 替代 setTimeout — 固定延时即使断言已经满足也要等满时间,
    // 而且断言若永远不满足也只是 silently resolve。waitFor 轮询期望状态, 立即返回。
    await waitFor(() => {
      expect(mockSearchStocks).not.toHaveBeenCalled();
      expect(onSelect).toHaveBeenCalledWith({
        code: 'AAPL',
        name: 'Apple Inc.',
        market: 'us',
      });
    });
  });
});
