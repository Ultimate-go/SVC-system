<script setup>
import Icon from '../icons/Icon.vue'
defineProps({
  title: { type: String, default: '暂无数据' },
  description: { type: String, default: '' },
  icon: { type: String, default: 'info' },
})
</script>

<template>
  <div class="empty-state">
    <div class="icon"><Icon :name="icon" :size="28" /></div>
    <div class="title">{{ title }}</div>
    <div class="desc" v-if="description">{{ description }}</div>
    <div class="action"><slot /></div>
  </div>
</template>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 40px 20px;
  color: var(--text-3);
}
.icon {
  opacity: 0.5;
}
.title {
  font-size: 14px;
  color: var(--text-2);
}
.desc {
  font-size: 12px;
}
.action {
  margin-top: 4px;
}
</style>
