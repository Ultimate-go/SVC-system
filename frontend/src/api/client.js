import axios from 'axios'
import { ElMessage } from 'element-plus'

/**
 * 统一的 axios 实例。
 *
 * baseURL 留空 = 请求同源 /api/...，由 vite.config.js 的 proxy 转给后端。
 * 这样一次省掉两件事：不用配 baseURL，也不用依赖 CORS。
 */

/** 401 时该做什么由外部注入（见 main.js），避免循环 import。 */
let onUnauthorized = () => {}

export function setUnauthorizedHandler(fn) {
  onUnauthorized = fn
}

const api = axios.create({
  baseURL: '',
  // 上传要 SM4 加密 + 逐块承诺 + 全网更新，慢是正常的，超时给宽一点。
  timeout: 120000,
})

api.interceptors.request.use((cfg) => {
  const token = localStorage.getItem('vds_token')
  if (token) cfg.headers.Authorization = `Bearer ${token}`
  return cfg
})

api.interceptors.response.use(
  (resp) => resp,
  (err) => {
    const status = err.response?.status
    const detail = err.response?.data?.detail

    if (status === 401) {
      // 登录接口自己返回的 401 不该触发「会话失效」跳转（本来就在登录页）。
      if (!err.config?.skipAuthRedirect) onUnauthorized()
      ElMessage.error(detail || '登录已失效，请重新登录')
    } else if (status === 403) {
      // ★ 演示亮点：把后端的中文拒绝理由原样弹出来。
      //   解密被拒时后端会说「你仍然可以验证它的完整性 —— 验证是公开的」。
      ElMessage.error(detail || '没有权限')
    } else if (status === 413) {
      ElMessage.error(detail || '文件超过大小上限')
    } else if (detail) {
      ElMessage.error(detail)
    } else if (err.code === 'ECONNABORTED') {
      ElMessage.error('请求超时 —— 后端可能正在做模幂，稍后重试')
    } else {
      ElMessage.error('请求失败：' + (err.message || '未知错误'))
    }
    return Promise.reject(err)
  },
)

export default api
