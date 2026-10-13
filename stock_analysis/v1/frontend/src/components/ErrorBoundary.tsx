import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
  errorInfo: ErrorInfo | null;
}

/** React Error Boundary - 捕获子组件 render 错误, 显示降级 UI */
export class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null, errorInfo: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error, errorInfo: null };
  }

  override componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    this.setState({ errorInfo });
    if (import.meta.env.DEV) {
      console.error('[ErrorBoundary]', error, errorInfo);
    }
  }

  private handleReset = (): void => {
    // iter 115: 用 window.location.reload() 代替 setState — 单纯 setState({hasError: false})
    // 后 React 重新渲染子组件, 若子组件 render 仍抛错 (e.g. 依赖数据缺失、第三方库异常),
    // 会立即再次进入 error state, 出现 "重试 → 立刻又出错" 的循环。整页 reload 强制
    // 重新挂载组件树, 让用户能拿到新的模块状态 (e.g. 清空 localStorage 后重新加载)。
    window.location.reload();
  };

  override render(): ReactNode {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;
      return (
        <div className="min-h-screen flex items-center justify-center bg-bg p-6">
          <div className="max-w-lg bg-panel border border-red-800/60 rounded-lg p-6 shadow-2xl">
            <div className="flex items-center gap-3 mb-4">
              <div className="w-10 h-10 bg-red-900/40 rounded-full flex items-center justify-center">
                <svg className="w-6 h-6 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                    d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z" />
                </svg>
              </div>
              <div>
                <h1 className="text-lg font-semibold text-red-300">出现了一些问题</h1>
                <p className="text-xs text-slate-400 mt-0.5">组件渲染错误已被捕获</p>
              </div>
            </div>

            <div className="bg-slate-900/50 rounded p-3 mb-4 text-xs font-mono text-slate-400 max-h-32 overflow-y-auto">
              <div className="text-red-300 font-semibold mb-1">
                {this.state.error?.name}: {this.state.error?.message}
              </div>
              {import.meta.env.DEV && this.state.errorInfo?.componentStack && (
                <pre className="whitespace-pre-wrap text-slate-400">
                  {this.state.errorInfo.componentStack.split('\n').slice(0, 5).join('\n')}
                </pre>
              )}
              {!import.meta.env.DEV && (
                <p className="text-slate-400">
                  错误详情已记录，请稍后刷新页面或联系技术支持。
                </p>
              )}
            </div>

            <div className="flex gap-2">
              {/* iter 75: focus-visible 环 — 避免高对比主题下浏览器默认 outline 看不见。
                  不加 aria-label: 可见文本 "重试"/"刷新页面" 已经是合适的可访问名,
                  加了反而和测试 getByRole({ name: '刷新页面' }) 不匹配。 */}
              <button
                type="button"
                onClick={this.handleReset}
                className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm rounded
                           transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-400"
              >
                重试
              </button>
              <button
                type="button"
                onClick={() => window.location.reload()}
                className="px-4 py-2 bg-slate-700 hover:bg-slate-600 text-white text-sm rounded
                           transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-slate-400"
              >
                刷新页面
              </button>
            </div>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
