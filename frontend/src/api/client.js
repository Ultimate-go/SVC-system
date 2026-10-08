import axios from 'axios'
import { ElMessage } from 'element-plus'

/**
 * 把后端 `detail` 变成**一句人话**。
 *
 * ★★ 为什么需要它：FastAPI 的**参数校验**错误（422）里 ``detail`` 是
 *   一个**数组**（``[{type, loc, msg, input, ctx}, …]``），而 Element Plus
 *   的 message 只接受字符串 / VNode ⇒ 直接塞进去弹出来的不是原因，
 *   是一团乱码（形如 `[object Object]` 或裸数组）。
 *
 *   最容易撞上的场景：一次 ``POST /api/query`` 带超过 8192 个下标 ——
 *   块数**没有上限**，所以“拿一份大文件的全部块去入池”就会走到这里
 *   （审计 F1）。归一之后至少能看见“indices：List should have at most
 *   8192 items”。
 *
 *   数组形态取每条 ``msg`` 拼起来，并带上字段路径 —— 不带路径的话，
 *   “at most 8192 items” 根本不知道说的是哪个字段。
 *
 * （导出它是为了能被测试钉住：``frontend/tests/apiDetail.test.js``。）
 */
export function readableDetail(detail) {
  if (detail == null) return ''
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail.map((d) => {
      if (d == null) return ''
      if (typeof d === 'string') return d
      const where = Array.isArray(d.loc)
        ? d.loc.filter((x) => x !== 'body' && x !== 'query' && x !== 'path').join('.')
        : ''
      const msg = d.msg || d.message || JSON.stringify(d)
      return where ? `${where}：${msg}` : msg
    })
    return parts.filter(Boolean).join('；')
  }
  if (typeof detail === 'object') {
    return detail.msg || detail.message || detail.detail || JSON.stringify(detail)
  }
  return String(detail)
}

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
    // ★ 归一成一句人话（后端 422 的 detail 是数组，见 readableDetail）
    const detail = readableDetail(err.response?.data?.detail)

    // ★★ 归一要**全局生效**（审计 N8）：以前只归一拦截器自己要弹的那条 toast，
    //   而视图层大量 `catch` 直接把 `e?.response?.data?.detail` 摆到界面上
    //   （全项目 35 处）—— 一旦 422 走到那些分支，屏幕上就是一坨 JSON 数组。
    //   所以这里**就地换掉** `detail`（原始值另存 `rawDetail` 备查）。
    //   已核实：全项目只有 readableDetail 自己依赖数组形态，换掉不会破坏谁。
    if (err.response?.data && err.response.data.detail != null) {
      err.rawDetail = err.response.data.detail
      err.response.data.detail = detail || err.response.data.detail
    }

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
      ElMessage.error(detail || '无访问权限')
    } else if (status === 413) {
      ElMessage.error(detail || '文件超出大小上限')
    } else if (detail) {
      ElMessage.error(detail)
    } else if (err.code === 'ECONNABORTED') {
      ElMessage.error('请求超时。若后端正在执行模幂运算，请延后重试')
    } else {
      ElMessage.error('请求失败：' + (err.message || '未知错误'))
    }
    return Promise.reject(err)
  },
)

export default api
