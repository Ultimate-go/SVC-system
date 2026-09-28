import { computed } from 'vue'
import { useAuthStore } from '../stores/auth'

/**
 * 权限相关。★ 前端不做安全判断 —— 这里只提供「看不见」级别的辅助，
 * 真正的权限在路由 meta + 后端。
 *
 * 判据只有一处：`stores/auth.js` 的 isAdmin / isLoggedIn getter。
 * 这个 composable 存在的意义是让「隐藏菜单 / 隐藏按钮」有个统一入口，
 * 而不是让每个组件各写一遍 `computed(() => auth.user?.role === 'admin')`
 * —— 那种写法已经在 Sidebar / Topbar 下拉 / 设备页 / 总览页各出现过一次，
 * 四处各写一遍就是四处可能写错。
 */
export function usePermission() {
  const auth = useAuthStore()
  const isAdmin = computed(() => auth.isAdmin)
  const isLoggedIn = computed(() => auth.isLoggedIn)
  return { isAdmin, isLoggedIn }
}
