/**
 * 全局路由守卫。全站只此一套，页面内不得重复实现。
 *
 * 规则：
 *  - meta.public         免登录（/login、错误页）
 *  - 已登录访问 /login   重定向到首页
 *  - 未登录访问受保护页  → /login（带 redirect）
 *  - meta.requiresAdmin 且非管理员 → /403
 *  - 设 document.title
 */
export function setupGuard(router) {
  router.beforeEach((to) => {
    const token = localStorage.getItem('vds_token')
    // 不变式（安全审计 N1）：`vds_user` 被写坏或写入非法 JSON 时不得抛出异常。
    //   路由守卫在导航前执行，异常会导致整站白屏且无提示。
    //   此处按未登录处理，并清除该键。
    let user = null
    try {
      user = JSON.parse(localStorage.getItem('vds_user') || 'null')
    } catch {
      try {
        localStorage.removeItem('vds_user')
      } catch {
        /* 隐私模式下 localStorage 可能抛出异常，忽略 */
      }
    }

    document.title = to.meta?.title ? `${to.meta.title} · VDS` : 'VDS'

    if (to.meta?.public) {
      // 已登录访问登录页：重定向到首页
      if (to.name === 'login' && token) return { path: '/' }
      return true
    }

    if (!token) {
      return { path: '/login', query: { redirect: to.fullPath } }
    }

    if (to.meta?.requiresAdmin && user?.role !== 'admin') {
      return { path: '/403' }
    }

    // 总览对全体用户开放，落点不再按角色区分。
    //   （旧行为：非管理员从 / 重定向到 /files。）
    return true
  })
}
