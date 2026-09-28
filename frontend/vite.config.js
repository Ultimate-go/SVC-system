import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端地址：默认 8000。换端口时设环境变量 VDS_API_TARGET 就行，不用改代码。
const API_TARGET = process.env.VDS_API_TARGET || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    // 端口被占时直接报错，不要自动换端口。
    strictPort: true,
    proxy: {
      // 前端一律请求同源的 /api/...，由 Vite 转给后端。
      '/api': { target: API_TARGET, changeOrigin: true },
    },
  },
})
