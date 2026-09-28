<script setup>
/**
 * 面包屑 —— 轻量，只显示当前页名（本项目页面层级浅，不做完整路径）。
 */
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const title = computed(() => route.meta?.title || '')
</script>

<template>
  <div class="breadcrumb" v-if="title">
    <span class="sep">/</span>
    <span class="cur">{{ title }}</span>
  </div>
</template>

<style scoped>
.breadcrumb {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: var(--text-3);
}
.cur {
  color: var(--text-2);
}
</style>
