import { defineConfig } from 'vite'

// 见微 Alpha 前端构建配置
// - dev(serve):  base '/'，应用跑在 http://localhost:5173/，/api 等代理到实时 Flask :5001
// - build:       base '/static/dist/'，产物落到 ../static/dist，由 Flask 的 /static 静态处理器提供
const PROXY_TARGET = 'http://127.0.0.1:5001'
const proxy = {
  '/api':     { target: PROXY_TARGET, changeOrigin: true },
  '/logs':    { target: PROXY_TARGET, changeOrigin: true },
  '/apidocs': { target: PROXY_TARGET, changeOrigin: true },
  '/flasgger_static': { target: PROXY_TARGET, changeOrigin: true },
}

export default defineConfig(({ command }) => ({
  root: '.',
  base: command === 'build' ? '/static/dist/' : '/',
  build: {
    outDir: '../static/dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 2000,
  },
  server: {
    host: '127.0.0.1',   // 绑 IPv4，避免预览浏览器走 127.0.0.1 而 Vite 只在 ::1 监听
    port: 5173,
    strictPort: false,
    // dev 下 /static(非 lib) 也代理到 Flask；/lib、/app 由 Vite public 提供
    proxy: { ...proxy, '/static': { target: PROXY_TARGET, changeOrigin: true } },
  },
  preview: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: false,
    // preview 下 Vite 在 base=/static/dist 提供产物，故不代理 /static（避免与产物路径冲突）
    proxy,
  },
}))
