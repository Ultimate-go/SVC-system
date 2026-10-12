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
    // ★ 坏数据不能让整站白屏（安全审计 N1）：`vds_user` 被写坏（或被人为写入
    //   非法 JSON）时，原先这里会直接抛异常，而路由守卫在**导航之前**执行 ——
    //   后果是整站白屏、且没有任何提示。现在视为“未登录”并把它清掉。
    let user = null
    try {
      user = JSON.parse(localStorage.getItem('vds_user') || 'null')
    } catch {
      try {
        localStorage.removeItem('vds_user')
      } catch {
        /* 隐私模式下 localStorage 可能直接抛 —— 忽略 */
      }
    }

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

    // ★ 2026-10-12：总览改为**对全体用户开放**，因此不再按角色换落点。
    //   （旧的写法是把非管理员从 / 送去 /files，属于“管理员首页”那套设定。）
    return true
  })
}
