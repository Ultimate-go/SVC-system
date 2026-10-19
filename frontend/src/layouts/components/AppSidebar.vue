<script setup>
/**
 * 侧栏菜单。
 *
 * ★ 前端不做安全判断：菜单隐藏只是「看不见」，真正的权限在后端。
 *   普通用户看不到管理员菜单项（`adminOnly`；对应后端的 `require_admin`）。
 */
import { computed, ref, watch } from 'vue'
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
 *
 * 顺序（2026-10-16 定）：总览 → 上传文件 → 可验证查询 → 增量聚合与分解 → 更新文件。
 *   「上传文件」后面直接跟「可验证查询」与「增量聚合与分解」：传上去就要能验、
 *   能在聚合面板里看；「更新文件」是同一件事的下一半，但用到的时机晚，排在增量聚合与分解之后。
 * ★ 2026-10-16：「性能」从核心菜单里去掉 —— 它的内容整体搬进了「总览」。
 * ★ 2026-10-16：「存储节点」也从核心菜单里去掉，改挂在**管理员工作台**下面。
 *   理由：它不是普通用户的操作面（看一眼哪台在线并不能改变什么），而调台数与端口
 *   本来就是管理员动作；演示 也从「登录管理员 → 初始化与系统自检」开始，节点那页跟在管理组里更顺。
 */
const core = {
  items: [
    { path: '/', name: 'dashboard', icon: 'dashboard', label: '总览', exact: true },
    { path: '/files', name: 'files', icon: 'file', label: '上传文件' },
    { path: '/evidence/query', name: 'query', icon: 'shield', label: '可验证查询' },
    { path: '/evidence/aggregate', name: 'aggregate', icon: 'list', label: '增量聚合与分解' },
    { path: '/files/update', name: 'files-update', icon: 'refresh', label: '更新文件' },
  ],
}

/** 管理员工作台：一个可展开的入口，展开后是它下面的四页。 */
const adminConsole = {
  label: '管理员工作台',
  icon: 'lock',
  items: [
    // ★ 2026-10-16 新增：原总览里的初始化现状与密钥模型都收在这一页，
    //   初始化流水线与参数选择也在这一页的最上面。
      { path: '/init', name: 'init', icon: 'key', label: '系统初始化' },
    // ★ 2026-10-16 从核心菜单移过来（菜单名与页面标题统一为「存储节点」）。
    { path: '/devices', name: 'devices', icon: 'server', label: '存储节点' },
    { path: '/users', name: 'users', icon: 'users', label: '用户管理' },
    { path: '/audit', name: 'audit', icon: 'activity', label: '审计流水' },
  ],
}

function isActive(item) {
  if (item.exact) return route.path === '/'
  if (item.name === 'devices') return route.path.startsWith('/devices')
  if (item.name === 'files') return route.path.startsWith('/files') && !route.path.startsWith('/files/update')
  if (item.name === 'files-update') return route.path.startsWith('/files/update')
  if (item.name === 'users') return route.path.startsWith('/users')
  return route.name === item.name
}

/**
 * 控制台展开态。
 *
 * ★ 默认跟随当前页面：直接进「用户管理」或「审计流水」（点链接或改 URL）时
 *   自动展开，不会出现“页在里面、侧栏却收着”的样子。
 * ★ 只单向自动展开（变成活跃时展开），不反向收起 —— 手动收起后要保持收起。
 */
const consoleOpen = ref(false)
const consoleActive = computed(() => adminConsole.items.some((i) => isActive(i)))
watch(
  consoleActive,
  (v) => {
    if (v) consoleOpen.value = true
  },
  { immediate: true },
)

function toggleConsole() {
  consoleOpen.value = !consoleOpen.value
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
      <button
        v-for="item in core.items"
        :key="item.path"
        class="nav-item"
        :class="{ active: isActive(item) }"
        :title="item.label"
        @click="go(item)"
      >
        <Icon :name="item.icon" :size="17" />
        <span class="nav-item-txt">{{ item.label }}</span>
      </button>

      <!-- 管理员工作台：点标题行展开 / 收起，展开后是它下面的三页。 -->
      <template v-if="isAdmin">
        <div class="nav-divider" />
        <button
          class="nav-item nav-parent"
          :class="{ active: consoleActive, open: consoleOpen }"
          :title="adminConsole.label"
          :aria-expanded="consoleOpen ? 'true' : 'false'"
          @click="toggleConsole"
        >
          <Icon :name="adminConsole.icon" :size="17" />
          <span class="nav-item-txt">{{ adminConsole.label }}</span>
          <Icon class="nav-caret" name="chevronRight" :size="13" />
        </button>
        <div class="nav-sub" :class="{ open: consoleOpen }">
          <button
            v-for="item in adminConsole.items"
            :key="item.path"
            class="nav-item nav-child"
            :class="{ active: isActive(item) }"
            :title="item.label"
            @click="go(item)"
          >
            <Icon :name="item.icon" :size="17" />
            <span class="nav-item-txt">{{ item.label }}</span>
          </button>
        </div>
      </template>
    </nav>


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
  /* ★ 2026-10-19：背景图组件是 `position: fixed; z-index: 0`，而侧栏是静态定位 ——
     默认绘在它**下面**，于是网格会盖到侧栏上（菜单那一条带网格）。
     这里抬一层，左侧背景就是干净的实心面板。 */
  position: relative;
  z-index: 1;
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
/* 分组分隔：原来这里是「核心」「管理」两行小标题（2026-10-14 去掉文字），
   现在只留一条细线 —— 分组感还在，但不占一行字的高度。
   第一组（核心）在栏顶，不需要线。 */
.nav-divider {
  height: 1px;
  margin: 12px 12px 10px;
  background: var(--line);
}
.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 10px 12px;
  margin-bottom: 2px;
  border: none;
  background: transparent;
  color: var(--text-2);
  font-size: 14px;
  font-family: inherit;
  cursor: pointer;
  border-radius: var(--radius-sm);
  text-align: left;
  position: relative;
  /* 悬停时这一格向右微微浮出：transform + 阴影，transition 覆盖所有会动的属性 */
  transition: background 0.18s ease, color 0.18s ease, padding 0.2s ease,
    transform 0.18s cubic-bezier(0.22, 0.61, 0.36, 1), box-shadow 0.18s ease;
}
.nav-item > svg {
  flex-shrink: 0;
  transition: transform 0.18s ease;
}
.nav-item:hover {
  background: rgba(120, 190, 255, 0.09);
  color: var(--text-1);
  transform: translateX(3px);
  box-shadow: 0 3px 12px rgba(0, 0, 0, 0.3), inset 0 0 0 1px rgba(120, 190, 255, 0.14);
}
.nav-item:hover > svg {
  transform: scale(1.08);
}
.nav-item:active {
  transform: translateX(3px) scale(0.985);
}
.nav-item.active {
  background: linear-gradient(90deg, rgba(34, 211, 238, 0.18), rgba(34, 211, 238, 0.05));
  color: var(--accent);
  transform: translateX(4px);
  box-shadow: 0 3px 14px rgba(34, 211, 238, 0.14), inset 0 0 0 1px rgba(34, 211, 238, 0.24);
}
/* 左侧那道竖条：从“缩起来且透明”长到“满高”，选中时看得见变化过程 */
.nav-item::before {
  content: '';
  position: absolute;
  left: 0;
  top: 6px;
  bottom: 6px;
  width: 3px;
  background: var(--accent);
  border-radius: 2px;
  opacity: 0;
  transform: scaleY(0.35);
  transition: opacity 0.18s ease, transform 0.18s ease;
}
.nav-item.active::before {
  opacity: 1;
  transform: scaleY(1);
}

/* 管理员工作台：标题行右侧的箭头，展开时转向下方 */
.nav-caret {
  margin-left: auto;
  flex-shrink: 0;
  opacity: 0.6;
  transition: transform 0.2s ease, opacity 0.18s ease;
}
.nav-parent.open .nav-caret {
  transform: rotate(90deg);
  opacity: 1;
}

/* 展开容器：用 max-height 过渡（不依赖子项个数，也不受 grid 子元素限制）。
   ★ 这个上限是**过渡用的假上限**，必须比展开后的真实高度大 —— 它不会
     自动量，写小了就是把最后几项裁掉。四页 × (约 40px + 2px 边距) ≈ 170px，
     原来写 140px，第四个「审计流水」只露出半截（实测）。留到 240px。 */
.nav-sub {
  max-height: 0;
  overflow: hidden;
  opacity: 0;
  transition: max-height 0.22s ease, opacity 0.18s ease;
}
.nav-sub.open {
  max-height: 240px;
  opacity: 1;
}
/* 子项相对父项缩进一格（收起成图标轨时会被下面那条规则改回同一中线） */
.nav-child {
  padding-left: 34px;
}
.sidebar-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border-top: 1px solid var(--line);
  font-size: 13px;
  color: var(--text-3);
}

/* 文字件：收起时收到 0 宽并透明（**不是 display:none** —— 那样子元素会从流里
   消失，图标没法稳定居中，展开时也会"跳"一下）。
   ★ 用 max-width 而不是 width：可过渡，且不会和 flex 争宽度。
   ★ 必须 display:inline-block：inline 元素上 width/max-width 是不生效的。 */
.brand-name,
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
.sidebar:not(.open) .nav-item-txt {
  max-width: 0;
  opacity: 0;
}
/* 图标轨里的分隔线：内边距对齐图标中线那一列（与 .nav 的 8px 相加 = 16px） */
.sidebar:not(.open) .nav-divider {
  margin-left: 16px;
  margin-right: 16px;
}
/* 图标轨里放不下箭头：标题行那个箭头隐藏（展开/收起的开关仍在右上角） */
.sidebar:not(.open) .nav-caret {
  display: none;
}

/* 关掉动效的人（或系统设置）：一律不做位移动画，只保留颜色变化。 */
@media (prefers-reduced-motion: reduce) {
  .nav-item,
  .nav-item > svg,
  .nav-item::before,
  .nav-caret,
  .nav-sub {
    transition: none;
  }
  .nav-item:hover,
  .nav-item.active,
  .nav-item:active {
    transform: none;
  }
}
</style>
