<script setup>
// 背景网格：两层 repeating-linear-gradient（20px 一格），中心一枚极淡径向渐晕。
// 不要动效。
</script>

<template>
  <div class="tech-backdrop" aria-hidden="true" />
</template>

<style scoped>
.tech-backdrop {
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background:
    radial-gradient(ellipse 70% 50% at 50% 0%, rgba(34, 211, 238, 0.05), transparent 70%),
    repeating-linear-gradient(0deg, var(--line) 0, var(--line) 1px, transparent 1px, transparent 20px),
    repeating-linear-gradient(90deg, var(--line) 0, var(--line) 1px, transparent 1px, transparent 20px);
  opacity: 0.5;
}
</style>
