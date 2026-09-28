import { defineStore } from 'pinia'
import api from '../api/client'

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
      return data
    },
    /** 本地清除（不调后端，供 401 兜底跳转时用）。 */
    clear() {
      this.setToken('')
      this.setUser(null)
    },
  },
})
