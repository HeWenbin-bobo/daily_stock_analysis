import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// Vite 配置：
// - 5173 端口（与后端 CORS 白名单一致）
// - 代理 /api 到后端 8001 端口
// - 同时配置 server.proxy（dev 用）+ preview.proxy（preview 用）
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
    },
  },
  // ★ Round 9 修复：vite preview 也需要 proxy（生产 preview 不读 server.proxy）
  preview: {
    port: 5173,
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8001',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // ★ 迭代5 功能升级：manualChunks 拆分稳定第三方依赖为独立 vendor chunk。
    //   修复生产构建 "chunks larger than 500 kB" 警告（此前主 chunk 861KB + 动态
    //   chunk 1134KB）：vue/naive-ui/axios 等不常变的依赖单独打包 → 浏览器可长期
    //   缓存 vendor，业务代码更新时无需重新下载整个依赖树。
    //   注意：echarts 已在 AdvancedViz.vue 用 `await import('echarts')` 按需懒加载
    //   （vite 会自动拆成独立 chunk），这里不再重复拆分。
    rollupOptions: {
      output: {
        manualChunks: {
          'vue-vendor': ['vue'],
          'naive-ui': ['naive-ui'],
          'axios': ['axios'],
        },
      },
    },
  },
})
