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

    // ★ ``silent``：调用方自己会把这条错误**完整**呈现出来（比如用对话框显示
    //   后端那段好几句的中文解释），拦截器就别再弹一个被截断的 toast 了。
    //   典型场景：改服务器台数时的 409（后端在 detail 里列了哪几块要搬、
    //   哪几块搬不动、该怎么办）。见 api/deploy.js。
    if (err.config?.silent) return Promise.reject(err)

    if (status === 401) {
      // ★★ 401 分两种，处理方式完全不同（这里曾经一律弹 toast，于是「后端重启后
      //    第一次打开页面」会先弹一句吓人的「令牌无效」）——
      //
      //    ① 登录接口自己返回的（skipAuthRedirect）= 口令错了。
      //       必须把后端那句话原样弹出来，用户才知道是密码问题；
      //    ② 其余一切 401 = **这次会话已经没了**：令牌过期（12 小时），
      //       或后端重启后换了一把 JWT 签名密钥（``backend/config.py`` 的
      //       ``secret_key`` 每次启动随机生成，这是设计如此，不是故障）。
      //       这两种都不是「出错」，用户唯一的动作就是重新登录 —— 所以
      //       **不弹**，安静地清干净并把他送回登录页；为什么被送回来，
      //       由登录页上那条提示说明（标记见 main.js 的 ``expired=1``）。
      if (err.config?.skipAuthRedirect) {
        ElMessage.error(detail || '登录失败')
      } else {
        onUnauthorized()
      }
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
