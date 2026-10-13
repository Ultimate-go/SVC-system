import { computed } from 'vue'
import { useAuthStore } from '../stores/auth'

/**
 * 权限辅助。前端不承担安全判定：此处只提供隐藏级别的辅助，
 * 真正的权限判定在路由 meta 与后端。
 *
 * 判据只有一处：`stores/auth.js` 的 isAdmin / isLoggedIn。
 * 本 composable 为「隐藏菜单 / 隐藏按钮」提供统一入口，
 * 避免各组件重复实现角色判断。
 */
export function usePermission() {
  const auth = useAuthStore()
  const isAdmin = computed(() => auth.isAdmin)
  const isLoggedIn = computed(() => auth.isLoggedIn)
  return { isAdmin, isLoggedIn }
}
