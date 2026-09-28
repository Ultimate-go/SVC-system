<script setup>
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import { usePermission } from '../../composables/usePermission'
import Icon from '../../components/icons/Icon.vue'

const auth = useAuthStore()
const router = useRouter()

const display = computed(() => auth.user?.display_name || auth.user?.username || '用户')
const { isAdmin } = usePermission()

function go(path) {
  router.push(path)
}

async function logout() {
  try {
    await auth.logout()
    router.replace('/login')
  } catch {
    // 即使后端 logout 失败，也本地清除并跳转。
    auth.clear()
    router.replace('/login')
  }
}
</script>

<template>
  <el-dropdown trigger="click" @command="go">
    <span class="user-chip">
      <span class="avatar">{{ display.slice(0, 1) }}</span>
      <span class="name">{{ display }}</span>
      <Icon name="user" :size="14" />
    </span>
    <template #dropdown>
      <el-dropdown-menu>
        <el-dropdown-item command="/profile">个人中心</el-dropdown-item>
        <el-dropdown-item command="/settings">界面偏好</el-dropdown-item>
        <el-dropdown-item divided @click="logout">退出登录</el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>
</template>

<style scoped>
.user-chip {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  color: var(--text-1);
  font-size: 13px;
  outline: none;
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
</style>
