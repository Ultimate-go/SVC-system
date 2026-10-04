<script setup>
/**
 * 侧栏菜单。
 *
 * ★ 前端不做安全判断：菜单隐藏只是「看不见」，真正的权限在 meta + 后端。
 *   普通用户看不到管理员菜单项（requiresAdmin）。
 */
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { usePermission } from '../../composables/usePermission'
import Icon from '../../components/icons/Icon.vue'

const { isAdmin } = usePermission()
const route = useRoute()
const router = useRouter()

/**
 * 侧栏开合：**点击**切换（不跟随鼠标悬停），并记住上次的选择。
 *
 * ★ 只存 localStorage 一个键，不动后端、也不进「界面偏好」页 ——
 *   它是个手感开关，不是主题的一部分（那个页只管主题/密度/动效/哈希详略）。
 * ★ 默认收起（图标轨）：内容区默认宽 160px，要看菜单名就点一下箭头。
 */
const OPEN_KEY = 'vds_sidebar_open'
const open = ref(localStorage.getItem(OPEN_KEY) === '1')
function toggle() {
  open.value = !open.value
  localStorage.setItem(OPEN_KEY, open.value ? '1' : '0')
}

/*
 * 菜单顺序：**核心在上、管理在下**（日常干活的那几页先够得着，
 * 管理类页面进去的频次低）。
 * ★ 数组顺序就是界面顺序 —— 这里换位不需要动 routes.js。
 */
const groups = [
  {
    label: '核心',
    items: [
      { path: '/files', name: 'files', icon: 'file', label: '文件与块' },
      { path: '/evidence/verify', name: 'verify', icon: 'shield', label: '完整性验证' },
      { path: '/evidence/pool', name: 'pool', icon: 'list', label: '证据池' },
      { path: '/devices', name: 'devices', icon: 'server', label: '设备' },
    ],
  },
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
  <!--
    侧栏默认是一条宽的「图标轨」（只有图标 + 分组细线），点右上角箭头展开。
    ★ 开合 = **点击**（不跟随鼠标悬停），并记住上次选择。
    ★ 它**在流里正常占位**：展开时把右侧内容推过去，不会盖住内容。
    ★ 每一格都带 title —— 收起时鼠标停上去能看全名字（不用等展开）。
  -->
  <aside class="sidebar" :class="{ open }">
    <div class="brand">
      <span class="brand-mark">VDS</span>
      <span class="brand-name">可验证分布式存储</span>
      <button
        class="rail-toggle"
        :title="open ? '收起侧栏' : '展开侧栏'"
        :aria-expanded="open ? 'true' : 'false'"
        @click="toggle"
      >
        <Icon :name="open ? 'chevronLeft' : 'chevronRight'" :size="16" />
      </button>
    </div>

    <nav class="nav">
      <template v-for="g in groups" :key="g.label">
        <template v-if="!g.adminOnly || isAdmin">
          <div class="nav-label"><span class="nav-label-txt">{{ g.label }}</span></div>
          <button
            v-for="item in g.items"
            :key="item.path"
            class="nav-item"
            :class="{ active: isActive(item) }"
            :title="item.label"
            @click="go(item)"
          >
            <Icon :name="item.icon" :size="16" />
            <span class="nav-item-txt">{{ item.label }}</span>
          </button>
        </template>
      </template>
    </nav>

    <div class="sidebar-foot mono">
      <Icon name="pulse" :size="13" />
      <span class="nav-item-txt" title="每份文件各占一段、各有自己的承诺与摘要，互不影响">一文件一向量</span>
    </div>
  </aside>
</template>

<style scoped>
/* 侧栏：**在流里正常占位**（不是覆盖层）。
   收起 = icon-rail，展开 = sidebar-w，由 .open 驱动（点击切换）。 */
.sidebar {
  width: var(--sidebar-rail);
  height: 100%;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-panel);
  border-right: 1px solid var(--line);
  overflow: hidden;
  transition: width 0.2s ease;
}
.sidebar.open {
  width: var(--sidebar-w);
}

.brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 16px 12px;
  border-bottom: 1px solid var(--line);
}
.brand-mark {
  font-family: var(--font-mono);
  font-weight: 500;
  color: var(--accent);
  font-size: 18px;
  letter-spacing: 0.05em;
  flex-shrink: 0;
}
.brand-name {
  font-size: 13px;
  color: var(--text-2);
}

/* 右上角那个开合箭头 —— 整个侧栏唯一的「机关」，必须一直看得见。 */
.rail-toggle {
  margin-left: auto;
  flex-shrink: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--text-2);
  cursor: pointer;
  transition: background 0.15s ease, color 0.15s ease;
}
.rail-toggle:hover {
  background: rgba(120, 190, 255, 0.08);
  color: var(--text-1);
}

.nav {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
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
  transition: background 0.15s ease, color 0.15s ease, padding 0.2s ease;
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

/* 文字件：收起时收到 0 宽并透明（**不是 display:none** —— 那样子元素会从流里
   消失，图标没法稳定居中，展开时也会"跳"一下）。
   ★ 用 max-width 而不是 width：可过渡，且不会和 flex 争宽度。
   ★ 必须 display:inline-block：inline 元素上 width/max-width 是不生效的。 */
.brand-name,
.nav-label-txt,
.nav-item-txt {
  display: inline-block;
  white-space: nowrap;
  overflow: hidden;
  max-width: 180px;
  transition: opacity 0.15s ease, max-width 0.2s ease;
}

/* ---------- 收起态（.sidebar 上没有 .open）：只剩图标，分组标题变一条细线 ---------- */
.sidebar:not(.open) .brand {
  /* 88 - 16 = 72 要装下 VDS(约 36) + 箭头(26)：内边距收到 8，间距归零 */
  gap: 0;
  padding-left: 8px;
  padding-right: 8px;
}
.sidebar:not(.open) .nav-item {
  gap: 0;
  /* 图标落在 88px 轨道正中：.nav 内边距 8 + 这里 32 + 图标半宽 8 = 44 = 88/2 */
  padding-left: 32px;
  padding-right: 0;
}
.sidebar:not(.open) .sidebar-foot {
  gap: 0;
  justify-content: center;
  padding-left: 0;
  padding-right: 0;
}
.sidebar:not(.open) .brand-name,
.sidebar:not(.open) .nav-label-txt,
.sidebar:not(.open) .nav-item-txt {
  max-width: 0;
  opacity: 0;
}
.sidebar:not(.open) .nav-label {
  height: 1px;
  padding: 0;
  margin: 12px 16px 10px;
  background: var(--line);
  overflow: hidden;
}
</style>
