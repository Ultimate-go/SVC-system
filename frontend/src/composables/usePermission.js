import { computed } from 'vue'
import { useAuthStore } from '../stores/auth'

/**
 * 权限相关。★ 前端不做安全判断，这里只提供「看不见」级别的辅助，
 * 真正的权限在 meta + 后端。
 */
export function usePermission() {
  const auth = useAuthStore()
  const isAdmin = computed(() => auth.user?.role === 'admin')
  const isLoggedIn = computed(() => auth.isLoggedIn)
  return { isAdmin, isLoggedIn }
}
