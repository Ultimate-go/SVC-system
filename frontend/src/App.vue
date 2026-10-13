<script setup>
import { onMounted } from 'vue'
import { useAuthStore } from './stores/auth'
import { useThemeStore } from './stores/theme'
import { useCryptoStore } from './stores/crypto'

const auth = useAuthStore()
const theme = useThemeStore()

// 应用启动：若已存在 token，则读取一次当前用户。会话态必须从接口读取，不得使用缓存。
//
// ★ 顺手把私钥接回来（`crypto.restore`）：刷新页面后 token 还在，
//   但私钥只活在内存里 —— 不接回来的话，顶栏会显示「未解锁」，
//   而用户刚刷新过，会以为“刷新就得重新登录”。
//   接回的条件：同一标签页、且上一次解锁在 5 分钟窗口内（见 stores/crypto.js）。
onMounted(() => {
  theme.apply()
  if (auth.token && !auth.user) {
    auth
      .fetchMe()
      .then((me) => {
        useCryptoStore().restore(me?.username || '')
      })
      .catch(() => {
        // 401 已由拦截器兜底处理。
      })
  }
})
</script>

<template>
  <router-view />
</template>
