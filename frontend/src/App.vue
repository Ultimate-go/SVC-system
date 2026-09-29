<script setup>
import { onMounted } from 'vue'
import { useAuthStore } from './stores/auth'
import { useThemeStore } from './stores/theme'

const auth = useAuthStore()
const theme = useThemeStore()

// 应用启动：若已有 token，拉一次当前用户（会话态必须是查出来的，不是缓存的）。
onMounted(() => {
  theme.apply()
  if (auth.token && !auth.user) {
    auth.fetchMe().catch(() => {
      // 401 已由拦截器兜底处理。
    })
  }
})
</script>

<template>
  <router-view />
</template>
