/**
 * 全局路由守卫 —— 只有这一套，不要在页面里各写一遍。
 *
 * 规则：
 *  - meta.public         免登录（/login、错误页）
 *  - 已登录访问 /login   送回首页
 *  - 未登录访问受保护页  → /login（带 redirect）
 *  - meta.requiresAdmin 且非管理员 → /403
 *  - 设 document.title
 */
export function setupGuard(router) {
  router.beforeEach((to) => {
    const token = localStorage.getItem('vds_token')
    const user = JSON.parse(localStorage.getItem('vds_user') || 'null')

    document.title = to.meta?.title ? `${to.meta.title} · VDS` : 'VDS'

    if (to.meta?.public) {
      // 已登录访问登录页 → 送回首页
      if (to.name === 'login' && token) return { path: '/' }
      return true
    }

    if (!token) {
      return { path: '/login', query: { redirect: to.fullPath } }
    }

    if (to.meta?.requiresAdmin && user?.role !== 'admin') {
      return { path: '/403' }
    }

    return true
  })
}
