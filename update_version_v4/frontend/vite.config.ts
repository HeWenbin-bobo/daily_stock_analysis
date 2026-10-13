import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// Vite 配置（v4 独立端口，后端端口可配）：
// - 5174 端口（与后端 CORS 白名单一致；被占用时自动 +1）
// - 代理 /api 到后端（默认 8002；VITE_API_TARGET 可覆盖，如 v4 与 v3 并存时用 8003）
// - 同时配置 server.proxy（dev 用）+ preview.proxy（preview 用）
const API_TARGET = process.env.VITE_API_TARGET || 'http://127.0.0.1:8002'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5174,
    host: '0.0.0.0',
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
  // ★ Round 9 修复：vite preview 也需要 proxy（生产 preview 不读 server.proxy）
  preview: {
    port: 5174,
    host: '127.0.0.1',
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
