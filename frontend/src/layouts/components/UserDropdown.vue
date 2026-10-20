<script setup>
/**
 * 顶栏账号菜单：点头像在下方弹出一个普通下拉框。
 *
 * ★ 为什么不用 `el-dropdown`（2026-10-20 改）：全站在 `body` 上用了 `zoom` 放大字号，
 *   而 Element Plus 的 popper 是 teleport 到 `body` 的 —— 它既被 `body` 的 zoom 再缩放
 *   一次，定位坐标又来自已含 zoom 的 `getBoundingClientRect`，两者叠加会让菜单跑到
 *   触发元素的左上方，看起来像把左边的内容挤走了。
 *   这里改成**在顶栏内部绝对定位**：它随页面一起缩放、不参与文档流，展开/收起都
 *   不会推动任何其它元素，也就没有定位误差。
 * ★ 关闭路径：点外部、按 Esc、选中任意一项、路由变化 —— 四条都要走 `close()`，
 *   否则菜单会挂在屏幕上。
 */
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import Icon from '../../components/icons/Icon.vue'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const display = computed(() => auth.user?.display_name || auth.user?.username || '用户')

const open = ref(false)
const root = ref(null)

function toggle() {
  open.value = !open.value
}
function close() {
  open.value = false
}

function go(path) {
  close()
  router.push(path)
}

async function logout() {
  close()
  try {
    await auth.logout()
    router.replace('/login')
  } catch {
    // 后端登出失败时同样清除本地状态并跳转。
    auth.clear()
    router.replace('/login')
  }
}

/** 点菜单以外的任何地方都收起（用 pointerdown，触屏也覆盖）。 */
function onDocDown(e) {
  if (!open.value) return
  if (root.value && !root.value.contains(e.target)) close()
}
function onKey(e) {
  if (e.key === 'Escape') close()
}

onMounted(() => {
  document.addEventListener('pointerdown', onDocDown)
  document.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', onDocDown)
  document.removeEventListener('keydown', onKey)
})

// 跳走之后菜单不该还留着（例如浏览器前进/后退触发路由变化）。
watch(() => route.fullPath, close)
</script>

<template>
  <div ref="root" class="user-menu">
    <button
      type="button"
      class="user-chip"
      aria-haspopup="menu"
      :aria-expanded="open"
      @click="toggle"
    >
      <span class="avatar">{{ display.slice(0, 1) }}</span>
      <span class="name">{{ display }}</span>
      <Icon name="user" :size="14" />
    </button>

    <transition name="drop">
      <div v-if="open" class="user-dropdown" role="menu">
        <button type="button" class="dd-item" role="menuitem" @click="go('/profile')">
          个人中心
        </button>
        <button type="button" class="dd-item" role="menuitem" @click="go('/settings')">
          界面偏好
        </button>
        <div class="dd-sep" aria-hidden="true" />
        <button type="button" class="dd-item is-danger" role="menuitem" @click="logout">
          退出登录
        </button>
      </div>
    </transition>
  </div>
</template>

<style scoped>
.user-menu {
  position: relative;
}
.user-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--text-1);
  font: inherit;
  font-size: 13px;
  cursor: pointer;
}
.user-chip:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 3px;
  border-radius: 6px;
}
.avatar {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  background: var(--bg-raised);
  border: 1px solid var(--line-strong);
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--accent);
  font-size: 13px;
}
.name {
  max-width: 140px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/*
 * 绝对定位在触发元素下方、右对齐：不参与文档流 ⇒ 展开时不会推动面包屑或任何其它块。
 * z-index 高过顶栏（10）与内容区，但低于弹窗（Element Plus 的 2000+）。
 */
.user-dropdown {
  position: absolute;
  top: calc(100% + 10px);
  right: 0;
  z-index: 80;
  min-width: 148px;
  padding: 6px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--bg-panel);
  box-shadow: 0 16px 36px rgba(0, 0, 0, 0.18);
}
.dd-item {
  display: block;
  width: 100%;
  padding: 8px 10px;
  border: 0;
  border-radius: 6px;
  background: none;
  color: var(--text-1);
  font: inherit;
  font-size: 13px;
  text-align: left;
  cursor: pointer;
}
.dd-item:hover {
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}
.dd-item.is-danger {
  color: var(--danger);
}
.dd-sep {
  height: 1px;
  margin: 5px 4px;
  background: var(--line);
}

.drop-enter-active,
.drop-leave-active {
  transition: opacity 0.14s ease, transform 0.14s ease;
}
.drop-enter-from,
.drop-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}
</style>
