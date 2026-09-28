<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '../../stores/auth'
import Icon from '../../components/icons/Icon.vue'
import UserDropdown from './UserDropdown.vue'
import KeyStatusTag from '../../components/security/KeyStatusTag.vue'

const auth = useAuthStore()
const route = useRoute()

const title = computed(() => route.meta?.title || '')
</script>

<template>
  <header class="topbar">
    <div class="left">
      <span class="page-title">{{ title }}</span>
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
}
.page-title {
  font-size: 15px;
  font-weight: 500;
  color: var(--text-1);
}
.right {
  display: flex;
  align-items: center;
  gap: 16px;
}
</style>
