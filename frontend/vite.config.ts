import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

/**
 * Vite 配置（开发文档 5.5 / 接口文档 7）。
 *
 * 代理为什么要配两条：
 *
 *   `/api`    —— 接口文档 §1.2，前端 dev 经 Vite 代理转发到 FastAPI。
 *   `/static` —— 接口文档 §5.5 的代理表只提了 /api，但 §4.2 的图片上传响应里
 *                url 是 `/static/uploads/img_xxx.png`（见 backend/setting.py 的
 *                static_url_prefix），引用卡片要看原图就必须走这个前缀。
 *                不代理的话 dev 下每张引用原图都会 404，且是 Vite 自己返回的 404，
 *                排查时极易误判成后端问题。
 *
 * 关于 SSE 首 Token 延迟：本代理默认不缓冲，开发期即可看到逐字输出。
 * 生产环境的坑在 nginx（接口文档 §8 缺口⑧：必须关 proxy_buffering 并设
 * X-Accel-Buffering: no），那是 deploy/nginx.conf 的职责，与本文件无关。
 */
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    // 与 backend/setting.py 的 cors_origins 对齐（后者同时放行 localhost 与 127.0.0.1）
    host: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/static': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
})
