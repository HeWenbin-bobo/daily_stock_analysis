/**
 * 应用入口模块。挂载 React 树, 外层包裹 ErrorBoundary 捕获渲染异常,
 * StrictMode 启用双次渲染检查 (iter 94 React 17+ JSX transform 优化)。
 */
// iter 94: 移除未使用的 React import — React 17+ JSX transform 不再需要 React 在作用域,
// 改用 named import createRoot, 进一步明确入口。
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import App from './App';
import { ErrorBoundary } from './components/ErrorBoundary';
import './index.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>
);