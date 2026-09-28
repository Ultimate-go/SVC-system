/**
 * 路由表 —— 路径的唯一出处。
 *
 * meta 约定：
 *  - public         免登录（只有 /login）
 *  - requiresAdmin  管理员才能进
 *  - title          页面标题（顶栏 + document.title）
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
        path: 'devices',
        name: 'devices',
        component: () => import('../views/devices/list/index.vue'),
        meta: { title: '设备' },
      },
      {
        path: 'devices/add',
        name: 'devices-add',
        component: () => import('../views/devices/add/index.vue'),
        meta: { title: '新增节点' },
      },
      {
        path: 'devices/:id',
        name: 'devices-detail',
        component: () => import('../views/devices/detail/index.vue'),
        meta: { title: '节点详情' },
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
        meta: { title: '文件与块' },
      },
      {
        path: 'files/:id',
        name: 'files-detail',
        component: () => import('../views/files/detail/index.vue'),
        meta: { title: '文件详情' },
      },
      {
        path: 'evidence/pool',
        name: 'pool',
        component: () => import('../views/evidence/pool/index.vue'),
        meta: { title: '证据池' },
      },
      {
        path: 'evidence/verify',
        name: 'verify',
        component: () => import('../views/evidence/verify/index.vue'),
        meta: { title: '完整性验证' },
      },
      {
        path: 'audit',
        name: 'audit',
        component: () => import('../views/admin/audit/index.vue'),
        meta: { requiresAdmin: true, title: '审计流水' },
      },
      {
        path: 'perf',
        name: 'perf',
        component: () => import('../views/admin/perf/index.vue'),
        meta: { title: '性能' },
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
