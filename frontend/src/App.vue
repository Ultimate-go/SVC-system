<script setup>
import { onMounted } from 'vue'
import { useAuthStore } from './stores/auth'
import { useThemeStore } from './stores/theme'

const auth = useAuthStore()
const theme = useThemeStore()

// 应用启动：若已存在 token，则读取一次当前用户。会话态必须从接口读取，不得使用缓存。
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
