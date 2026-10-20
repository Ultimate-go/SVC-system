/**
 * 路由表 —— 路径的唯一出处。
 *
 * meta 约定：
 *  - public         免登录（只有 /login）
 *  - requiresAdmin  管理员才能进
 *  - title          页面标题（顶栏 + document.title）
 *  - parent         可选，{ title, path } —— 详情/新增类页面的回退目标，
 *                   由顶栏的 AppBreadcrumb 渲染成可点的父级。
 *
 * ★ 业务红线：前端不做任何安全判断。菜单隐藏只是「看不见」，
 *   真正的权限在 meta + 后端。路由守卫只有一套（见 guard.js）。
 */

export const routes = [
  {
    path: '/login',
    component: () => import('../layouts/BlankLayout.vue'),
    meta: { public: true },
    children: [
      {
        path: '',
        name: 'login',
        component: () => import('../views/login/index.vue'),
        meta: { public: true, title: '登录' },
      },
    ],
  },
  {
    path: '/',
    component: () => import('../layouts/MainLayout.vue'),
    children: [
      {
        path: '',
        name: 'dashboard',
        component: () => import('../views/admin/dashboard/index.vue'),
        meta: { title: '总览' },
      },
      {
        path: 'users',
        name: 'users',
        component: () => import('../views/users/list/index.vue'),
        meta: { requiresAdmin: true, title: '用户管理' },
      },
      {
        path: 'users/add',
        name: 'users-add',
        component: () => import('../views/users/add/index.vue'),
        meta: { requiresAdmin: true, title: '添加用户' },
      },
      {
        path: 'users/:id',
        name: 'users-detail',
        component: () => import('../views/users/detail/index.vue'),
        meta: { requiresAdmin: true, title: '用户详情' },
      },
      {
        // ★ 2026-10-16：存储节点移到「管理员工作台」菜单下，路由同步加管理员限制
        //   （后端 /api/nodes 那一族本来就要管理员，这里只是让“看不见”与“打不开”一致）。
        path: 'devices',
        name: 'devices',
        component: () => import('../views/devices/list/index.vue'),
        meta: { requiresAdmin: true, title: '存储节点' },
      },
      {
        path: 'devices/add',
        name: 'devices-add',
        component: () => import('../views/devices/add/index.vue'),
        meta: {
          requiresAdmin: true,
          title: '新增节点',
          parent: { title: '存储节点', path: '/devices' },
        },
      },
      {
        path: 'devices/:id',
        name: 'devices-detail',
        component: () => import('../views/devices/detail/index.vue'),
        meta: { requiresAdmin: true, title: '节点详情' },
      },
      {
        path: 'profile',
        name: 'profile',
        component: () => import('../views/user/profile/index.vue'),
        meta: { title: '个人中心' },
      },
      {
        path: 'settings',
        name: 'settings',
        component: () => import('../views/user/settings/index.vue'),
        meta: { title: '界面偏好' },
      },
      {
        path: 'files',
        name: 'files',
        component: () => import('../views/files/list/index.vue'),
        meta: { title: '上传文件' },
      },
      {
        path: 'files/:id',
        name: 'files-detail',
        component: () => import('../views/files/detail/index.vue'),
        meta: { title: '文件详情' },
      },
      {
        // ★ 2026-10-12：「更新文件」从「文件详情」里拆出来 —— 先选文件，再进它的变更页。
        //   下面这一页是列表，再下面那条复用详情组件（meta.ops=true 决定“形态”）。
        path: 'files/update',
        name: 'files-update',
        component: () => import('../views/files/update/index.vue'),
        meta: { title: '更新文件' },
      },
      {
        path: 'files/update/:id',
        name: 'files-update-detail',
        component: () => import('../views/files/detail/index.vue'),
        meta: {
          title: '更新文件 · 详情',
          ops: true,
          parent: { title: '更新文件', path: '/files/update' },
        },
      },
      {
        path: 'evidence/aggregate',
        name: 'aggregate',
        component: () => import('../views/evidence/aggregate/index.vue'),
        meta: { title: '增量聚合与分解' },
      },
      {
        path: 'evidence/query',
        name: 'query',
        component: () => import('../views/evidence/query/index.vue'),
        meta: { title: '可验证查询' },
      },
      {
        path: 'audit',
        name: 'audit',
        component: () => import('../views/admin/audit/index.vue'),
        meta: { requiresAdmin: true, title: '审计流水' },
      },
      {
        // ★ 2026-10-16：「性能」页并入「总览」，这里换成「系统初始化」。
        //   搬过来的是原总览里的规模 / 系统参数 / 逐文件摘要 / 运行状态 / 密钥模型，
        //   外加「全局运行态势」（处理链路 + 点开的原理知识）与十项系统自检。它们回答的都是「这套系统初始化成什么样了」，
        //   与「现在跑得多快」不是一回事。
        path: 'init',
        name: 'init',
        component: () => import('../views/admin/init/index.vue'),
        meta: { requiresAdmin: true, title: '初始化与系统自检' },
      },
    ],
  },
  {
    path: '/403',
    name: 'forbidden',
    component: () => import('../views/error/403.vue'),
    meta: { public: true, title: '无权限' },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('../views/error/404.vue'),
    meta: { public: true, title: '未找到' },
  },
]
