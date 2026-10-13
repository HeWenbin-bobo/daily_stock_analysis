import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 端口刻意避开参考项目 daily_stock_analysis_v2 的 8000 / 5173
// 后端默认 8100, 前端默认 5179
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5179,
    host: '127.0.0.1',
    strictPort: false,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8100',
        changeOrigin: true,
      },
    },
  },
  // iter 71: 拆分 vendor chunk, 让 lightweight-charts (~120KB gzip) 独立缓存
  // 业务代码更新时不需要重新下载 vendor。React + zustand + axios 走 react-vendor。
  build: {
    target: 'es2020', // 与 tsconfig 对齐
    sourcemap: false, // 生产不暴露源码
    rollupOptions: {
      output: {
        manualChunks: {
          // 最大的单独库拆出来 — K线图更新不触发 vendor 重下
          'vendor-charts': ['lightweight-charts'],
          'vendor-react': ['react', 'react-dom'],
          'vendor-utils': ['axios', 'zustand'],
        },
      },
    },
  },
});