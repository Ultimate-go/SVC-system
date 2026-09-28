<script setup>
/**
 * 侧栏菜单。
 *
 * ★ 前端不做安全判断：菜单隐藏只是「看不见」，真正的权限在 meta + 后端。
 *   普通用户看不到管理员菜单项（requiresAdmin）。
 */
import { useRoute, useRouter } from 'vue-router'
import { usePermission } from '../../composables/usePermission'
import Icon from '../../components/icons/Icon.vue'

const { isAdmin } = usePermission()
const route = useRoute()
const router = useRouter()

const groups = [
  {
    label: '管理',
    adminOnly: true,
    items: [
      { path: '/', name: 'dashboard', icon: 'dashboard', label: '总览', exact: true },
      { path: '/users', name: 'users', icon: 'users', label: '用户管理' },
      { path: '/audit', name: 'audit', icon: 'activity', label: '审计流水' },
      { path: '/perf', name: 'perf', icon: 'chart', label: '性能' },
    ],
  },
  {
    label: '核心',
    items: [
      { path: '/files', name: 'files', icon: 'file', label: '文件与块' },
      { path: '/evidence/pool', name: 'pool', icon: 'list', label: '证据池' },
      { path: '/evidence/verify', name: 'verify', icon: 'shield', label: '完整性验证' },
      { path: '/devices', name: 'devices', icon: 'server', label: '设备' },
    ],
  },
]

function isActive(item) {
  if (item.exact) return route.path === '/'
  if (item.name === 'devices') return route.path.startsWith('/devices')
  if (item.name === 'files') return route.path.startsWith('/files')
  if (item.name === 'users') return route.path.startsWith('/users')
  return route.name === item.name
}

function go(item) {
  router.push(item.path)
}
</script>

<template>
  <aside class="sidebar">
    <div class="brand">
      <span class="brand-mark">VDS</span>
      <span class="brand-name">可验证分布式存储</span>
    </div>

    <nav class="nav">
      <template v-for="g in groups" :key="g.label">
        <template v-if="!g.adminOnly || isAdmin">
          <div class="nav-label">{{ g.label }}</div>
          <button
            v-for="item in g.items"
            :key="item.path"
            class="nav-item"
            :class="{ active: isActive(item) }"
            @click="go(item)"
          >
            <Icon :name="item.icon" :size="16" />
            <span>{{ item.label }}</span>
          </button>
        </template>
      </template>
    </nav>

    <div class="sidebar-foot mono">
      <Icon name="pulse" :size="13" />
      <span>全系统一条向量</span>
    </div>
  </aside>
</template>

<style scoped>
.sidebar {
  width: var(--sidebar-w);
  height: 100%;
  background: var(--bg-panel);
  border-right: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  flex-shrink: 0;
}
.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 16px;
  border-bottom: 1px solid var(--line);
}
.brand-mark {
  font-family: var(--font-mono);
  font-weight: 500;
  color: var(--accent);
  font-size: 18px;
  letter-spacing: 0.05em;
}
.brand-name {
  font-size: 13px;
  color: var(--text-2);
}
.nav {
  flex: 1;
  overflow-y: auto;
  padding: 12px 8px;
}
.nav-label {
  font-size: 11px;
  color: var(--text-3);
  text-transform: uppercase;
  letter-spacing: 0.08em;
  padding: 12px 12px 6px;
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 9px 12px;
  margin-bottom: 2px;
  border: none;
  background: transparent;
  color: var(--text-2);
  font-size: 13px;
  font-family: inherit;
  cursor: pointer;
  border-radius: var(--radius-sm);
  text-align: left;
  position: relative;
  transition: background 0.15s ease, color 0.15s ease;
}
.nav-item:hover {
  background: rgba(120, 190, 255, 0.05);
  color: var(--text-1);
}
.nav-item.active {
  background: rgba(34, 211, 238, 0.08);
  color: var(--accent);
}
.nav-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 6px;
  bottom: 6px;
  width: 3px;
  background: var(--accent);
  border-radius: 2px;
}
.sidebar-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-top: 1px solid var(--line);
  font-size: 11px;
  color: var(--text-3);
}
</style>
