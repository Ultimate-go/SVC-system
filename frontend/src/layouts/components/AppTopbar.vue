<script setup>
import { useAuthStore } from '../../stores/auth'
import UserDropdown from './UserDropdown.vue'
import KeyStatusTag from '../../components/security/KeyStatusTag.vue'
import AppBreadcrumb from './AppBreadcrumb.vue'

const auth = useAuthStore()
</script>

<template>
  <header class="topbar">
    <div class="left">
      <AppBreadcrumb />
    </div>
    <div class="right">
      <KeyStatusTag v-if="auth.user" :user="auth.user" />
      <UserDropdown />
    </div>
  </header>
</template>

<style scoped>
.topbar {
  height: var(--topbar-h);
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  background: var(--bg-panel);
  border-bottom: 1px solid var(--line);
  position: relative;
  z-index: 10;
}
.left {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.right {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-shrink: 0;
}
</style>
