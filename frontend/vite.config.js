import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端地址：默认 8000。换端口时设环境变量 VDS_API_TARGET 就行，不用改代码。
// （一键启动会按部署配置自动设好它，见 scripts/start_all.py 的 start_frontend。）
const API_TARGET = process.env.VDS_API_TARGET || 'http://127.0.0.1:8000'

// 前端自己的端口：默认 5173。
//
// ★ 权威来源是 nodes/deploy.json（管理员界面 → 设备页 → 端口），启动器用
//   `--port` 把它传进来（见 scripts/start_all.py 的 frontend_command）。
//   这里读 VDS_FRONTEND_PORT 只是给"手动 npm run dev"留一个口子：在界面上把
//   前端端口改成 6173 之后，手动起也得能对上 —— 否则页面开在 5173，
//   而界面与重启器按 6173 记，两边说的不是一件事。
const PORT = Number(process.env.VDS_FRONTEND_PORT) || 5173

export default defineConfig({
  plugins: [vue()],
  server: {
    host: '127.0.0.1',
    port: PORT,
    // 端口被占时直接报错，不要自动换端口。
    // ★ 这一条与"端口可配"是配套的：自动换端口会让配置说的和实际跑的不是
    //   同一个端口，而管理员界面上那些数字就全都对不上了。
    strictPort: true,
    proxy: {
      // 前端一律请求同源的 /api/...，由 Vite 转给后端。
      '/api': { target: API_TARGET, changeOrigin: true },
    },
  },
})
