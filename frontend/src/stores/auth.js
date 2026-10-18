import { defineStore } from 'pinia'
import api from '../api/client'
import { useCryptoStore } from './crypto'
import { useAggregateStore } from './aggregate'
import { useBasketStore } from './basket'
import { forgetActivity, markLogin } from '../utils/session'

const TOKEN_KEY = 'vds_token'
const USER_KEY = 'vds_user'

/**
 * 认证 store。只存 token 与用户；用户对象是登录 / /me 拿到的那个结构。
 *
 * ★ 两个字段必须分清（这是业务红线）：
 *   - has_key    库级：库里有没有密钥对（重启/退出后仍是 true）
 *   - session_key 会话级：本次会话能不能解密（后端重启/退出后变 false）
 * 顶栏要用 KeyStatusTag 同时挂两枚标签，不能只显示 has_key。
 */
export const useAuthStore = defineStore('auth', {
  state: () => ({
    token: localStorage.getItem(TOKEN_KEY) || '',
    user: null,
  }),
  getters: {
    isLoggedIn: (s) => !!s.token,
    isAdmin: (s) => s.user?.role === 'admin',
    username: (s) => s.user?.username || '',
  },
  actions: {
    setToken(token) {
      this.token = token || ''
      if (token) localStorage.setItem(TOKEN_KEY, token)
      else localStorage.removeItem(TOKEN_KEY)
    },
    setUser(user) {
      this.user = user
      // 缓存一份到 localStorage，供路由守卫轻量判断 role（真正的权限在后端）。
      if (user) localStorage.setItem(USER_KEY, JSON.stringify(user))
      else localStorage.removeItem(USER_KEY)
    },
    /**
     * 登录。返回 user（含 timings，供总览页展示登录耗时）。
     *
     * @param {string} username
     * @param {string} password
     * @param {{onUnlockProgress?: (fraction: number) => void}} [options]
     *        `onUnlockProgress` 是**解封私钥的真实进度**（0→1，耗时几乎全在
     *        派生 KEK 上）。登录页拿它画进度条 —— 真进度，不是装饰性的假动画。
     */
    async login(username, password, options = {}) {
      const { data } = await api.post('/api/auth/login', { username, password }, { skipAuthRedirect: true })
      this.setToken(data.token)
      this.setUser(data.user)
      // ★ 盖一个"这次登录"的戳：系统初始化页那条流水线靠它做到"每次登录只演一遍"
      //   （退出重登会刷新它 ⇒ 又能演一遍）。见 utils/session.js。
      markLogin()

      // ★ 登录成功后顺手把私钥在**浏览器**里解出来。
      //   口令就在手上（用户刚输过），所以不必让他再输一次；
      //   解出来只活在当前页面内存里（见 stores/crypto.js 的三条纪律）。
      //   解不开**不是**登录失败 —— 令牌已经拿到了，这里只影响“能不能解密”。
      //
      //   ★ 走**异步**那条（`unlockAsync`）：这一步是 20 万轮 PBKDF2-HMAC-SM3
      //     （纯 JS，2~3 秒）。同步算会把主线程占满 —— 登录页的等待动画
      //     会当场冻住。计算量与结果都没变，只是切成 10ms 一片、中途让出。
      const crypto = useCryptoStore()
      crypto.lockIfOther(username)
      if (data.key_blob) {
        try {
          await crypto.unlockAsync(password, data.key_blob, username, options.onUnlockProgress)
        } catch (e) {
          crypto.locked()
          crypto.error = e?.message || '解封私钥失败'
        }
      } else {
        // server_key=true 的对比路径：私钥在后端，浏览器这边没有可解的密文。
        crypto.locked()
      }
      return data
    },
    /** 拉取当前用户（每次刷新页面都调，会话态必须是查出来的）。 */
    async fetchMe() {
      const { data } = await api.get('/api/auth/me')
      this.setUser(data)
      return data
    },
    /**
     * 自助注册普通用户。
     *
     * 调 ``/api/auth/register``（后端强制 role=user，绝不会拿到管理员）。
     * 成功后**自动登录**：这样私钥会在浏览器本地用注册口令解封出来，
     * 进个人中心就能直接看到自己的 SM2 私钥（无需再单独登录一次）。
     *
     * @param {string} username
     * @param {string} password
     * @param {string} [displayName]
     */
    async register(username, password, displayName = '', options = {}) {
      await api.post('/api/auth/register', { username, password, display_name: displayName })
      // 注册成功 → 直接拿同一套口令登录，顺便把私钥解到本机内存。
      try {
        await this.login(username, password, options)
      } catch {
        // 注册本身已成功（账号已落库）。登录若失败也不影响账号创建，
        // 让登录页提示“注册成功，请手动登录”即可。
      }
    },
    async logout() {
      const { data } = await api.post('/api/auth/logout')
      this.setToken('')
      this.setUser(null)
      // ★ 浏览器里的私钥必须跟着一起丢 —— 否则“退出”只丢了令牌，
      //   而真正能解密的那一样东西还留在内存里。
      useCryptoStore().locked()
      this.forgetLocal()
      return data
    },
    /** 本地清除（不调后端，供 401 兜底跳转时用）。 */
    clear() {
      this.setToken('')
      this.setUser(null)
      useCryptoStore().locked()
      this.forgetLocal()
    },
    /**
     * 清掉**上一个账号**留在这个浏览器里的东西（安全审计 I3）。
     *
     * 增量聚合与分解（``vds_pool``，sessionStorage）与"看过的文件清单"
     * （``vds_basket``，**localStorage**）都是**按浏览器**存的，与账号无关。
     * 以前登出只清 token 与 user，于是同一台机器上换个账号登录，
     * 还能看到**上一个用户**的证据卡片与文件清单 —— 那不是"残留体验"，
     * 而是把别人的东西摆在了你面前。
     */
    forgetLocal() {
      // ★ 活动时刻也要清（会话时效）：不清的话，换了个人进来、
      //   盘上还留着上一个人的“最后活动”，5 分钟窗口会算错。
      forgetActivity()
      // store 还没初始化时（比如极端早的 401）不能把登出流程搞崩，
      // 所以各自 try 一次。
      try {
        useAggregateStore().clear()
      } catch {
        /* 忽略 */
      }
      try {
        useBasketStore().clear()
      } catch {
        /* 忽略 */
      }
      // ★ 锚也要清（安全审计 N4）：`vds_anchor_v1`（明文 SHA-256 记账）、
      //   `vds_delta_anchor_v1`（钉住的 δ 本体）与 `vds_crs_v1`（群参数）
      //   都是**按浏览器**存的、与账号无关。登出后留在本机，等于把
      //   上一个人的“见证起点”留给了下一个人，也让“登出”这件事不彻底。
      //   丢掉它们的代价只是“下次解密重新见证一次”。
      for (const key of ['vds_anchor_v1', 'vds_delta_anchor_v1', 'vds_crs_v1']) {
        try {
          localStorage.removeItem(key)
        } catch {
          /* 忽略 */
        }
      }
    },
  },
})
