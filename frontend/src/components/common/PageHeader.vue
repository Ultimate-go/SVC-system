<script setup>
defineProps({
  title: { type: String, required: true },
  subtitle: { type: String, default: '' },
})
</script>

<template>
  <div class="page-header">
    <div>
      <h2 class="title">{{ title }}</h2>
      <p class="subtitle" v-if="subtitle">{{ subtitle }}</p>
    </div>
    <div class="actions"><slot /></div>
  </div>
</template>

<style scoped>
.page-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}
.title {
  font-size: 16px;
  font-weight: 500;
  color: var(--text-1);
}
.subtitle {
  font-size: 13px;
  color: var(--text-2);
  margin-top: 4px;
}
.actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}
</style>
