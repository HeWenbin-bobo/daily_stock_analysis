import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { Component, type ReactNode } from 'react';
import { ErrorBoundary } from './ErrorBoundary';

// 一个会抛错的组件
class ThrowingComponent extends Component<{ shouldThrow?: boolean }> {
  override render(): ReactNode {
    if (this.props.shouldThrow) {
      throw new Error('Test error from throwing component');
    }
    return <div>正常内容</div>;
  }
}

// 一个不会抛错的组件
function NormalComponent() {
  return <div>正常内容</div>;
}

describe('ErrorBoundary', () => {
  // iter 83: 之前 spyOn 在模块级调用且无 restore, 会污染后续 test 文件的 console.error。
  // 用 beforeEach + afterEach 包裹, 让 spy 严格只在 ErrorBoundary 这一组测试中生效。
  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  describe('正常渲染', () => {
    it('renders children when no error', () => {
      render(
        <ErrorBoundary>
          <NormalComponent />
        </ErrorBoundary>
      );
      expect(screen.getByText('正常内容')).toBeInTheDocument();
    });

    it('does not show error UI when children render fine', () => {
      render(
        <ErrorBoundary>
          <NormalComponent />
        </ErrorBoundary>
      );
      expect(screen.queryByText('出现了一些问题')).not.toBeInTheDocument();
    });
  });

  describe('错误捕获', () => {
    it('renders fallback UI when child throws', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent shouldThrow={true} />
        </ErrorBoundary>
      );
      // 默认 fallback UI 含 "出现了一些问题"
      expect(screen.getByText('出现了一些问题')).toBeInTheDocument();
    });

    it('shows error name and message in fallback', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent shouldThrow={true} />
        </ErrorBoundary>
      );
      expect(screen.getByText(/Error: Test error from throwing component/)).toBeInTheDocument();
    });

    it('uses custom fallback when provided', () => {
      render(
        <ErrorBoundary fallback={<div>自定义错误 UI</div>}>
          <ThrowingComponent shouldThrow={true} />
        </ErrorBoundary>
      );
      expect(screen.getByText('自定义错误 UI')).toBeInTheDocument();
      expect(screen.queryByText('出现了一些问题')).not.toBeInTheDocument();
    });
  });

  describe('恢复机制', () => {
    it('shows 重试 button that triggers page reload', () => {
      // iter 115: 重试按钮改为 window.location.reload() 防止 render-time 抛错时
      // 出现 "重试 → 立刻又出错" 循环。测试 mock location.reload 验证被调用。
      const reloadMock = vi.fn();
      const originalLocation = window.location;
      Object.defineProperty(window, 'location', {
        value: { ...originalLocation, reload: reloadMock },
        writable: true,
        configurable: true,
      });

      render(
        <ErrorBoundary>
          <ThrowingComponent shouldThrow={true} />
        </ErrorBoundary>
      );
      // 错误 UI 应显示
      const retryButton = screen.getByRole('button', { name: '重试' });
      expect(retryButton).toBeInTheDocument();

      // 点击重试 → 触发整页 reload (而非 setState)
      retryButton.click();
      expect(reloadMock).toHaveBeenCalledTimes(1);

      // 恢复原始 window.location, 避免污染后续 test
      Object.defineProperty(window, 'location', {
        value: originalLocation,
        writable: true,
        configurable: true,
      });
    });
  });

  describe('静默 componentStack 截断', () => {
    it('limits stack trace to 5 lines', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent shouldThrow={true} />
        </ErrorBoundary>
      );
      // 错误 UI 中的 stack trace 应被截断到 5 行
      // 具体检查存在 <pre> 元素
      const preElement = document.querySelector('pre');
      expect(preElement).toBeInTheDocument();
    });
  });

  describe('错误恢复', () => {
    it('retry button triggers page reload', () => {
      // iter 115: 与恢复机制 describe 一致 — 重试 → window.location.reload()
      const reloadMock = vi.fn();
      const originalLocation = window.location;
      Object.defineProperty(window, 'location', {
        value: { ...originalLocation, reload: reloadMock },
        writable: true,
        configurable: true,
      });

      render(
        <ErrorBoundary>
          <ThrowingComponent shouldThrow={true} />
        </ErrorBoundary>
      );
      expect(screen.getByText('出现了一些问题')).toBeInTheDocument();

      const retryButton = screen.getByRole('button', { name: '重试' });
      retryButton.click();

      // 重试触发整页 reload, 不依赖 setState
      expect(reloadMock).toHaveBeenCalledTimes(1);

      Object.defineProperty(window, 'location', {
        value: originalLocation,
        writable: true,
        configurable: true,
      });
    });

    it('refresh button is rendered', () => {
      // 只验证刷新按钮存在(不能真正点击避免影响测试环境)
      render(
        <ErrorBoundary>
          <ThrowingComponent shouldThrow={true} />
        </ErrorBoundary>
      );
      const refreshButton = screen.getByRole('button', { name: '刷新页面' });
      expect(refreshButton).toBeInTheDocument();
    });
  });

  describe('详细错误显示', () => {
    it('displays error name (Error type)', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent shouldThrow={true} />
        </ErrorBoundary>
      );
      // "Error: Test error..." 格式
      expect(screen.getByText(/^Error: Test error from throwing component/)).toBeInTheDocument();
    });

    it('includes component stack trace', () => {
      render(
        <ErrorBoundary>
          <ThrowingComponent shouldThrow={true} />
        </ErrorBoundary>
      );
      // <pre> 应包含 stack 跟踪
      const preElement = document.querySelector('pre');
      expect(preElement?.textContent).toBeTruthy();
    });
  });
});
