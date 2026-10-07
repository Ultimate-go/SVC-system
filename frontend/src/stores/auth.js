import { defineStore } from 'pinia'
import api from '../api/client'
import { useCryptoStore } from './crypto'
import { usePoolStore } from './pool'
import { useBasketStore } from './basket'

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
    /** 登录。返回 user（含 timings，供总览页展示登录耗时）。 */
    async login(username, password) {
      const { data } = await api.post('/api/auth/login', { username, password }, { skipAuthRedirect: true })
      this.setToken(data.token)
      this.setUser(data.user)

      // ★ 登录成功后顺手把私钥在**浏览器**里解出来。
      //   口令就在手上（用户刚输过），所以不必让他再输一次；
      //   解出来只活在当前页面内存里（见 stores/crypto.js 的三条纪律）。
      //   解不开**不是**登录失败 —— 令牌已经拿到了，这里只影响“能不能解密”。
      const crypto = useCryptoStore()
      crypto.lockIfOther(username)
      if (data.key_blob) {
        try {
          crypto.unlock(password, data.key_blob, username)
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
     * 证据池（``vds_pool``，sessionStorage）与"看过的文件清单"
     * （``vds_basket``，**localStorage**）都是**按浏览器**存的，与账号无关。
     * 以前登出只清 token 与 user，于是同一台机器上换个账号登录，
     * 还能看到**上一个用户**的证据卡片与文件清单 —— 那不是"残留体验"，
     * 而是把别人的东西摆在了你面前。
     */
    forgetLocal() {
      // store 还没初始化时（比如极端早的 401）不能把登出流程搞崩，
      // 所以各自 try 一次。
      try {
        usePoolStore().clear()
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
