<script setup>
/**
 * CSS 横条 —— 一根横条按比例定宽。不引 ECharts。
 * props: value(当前值), max(最大值), tone(颜色), label(可选左侧标签)。
 */
const props = defineProps({
  value: { type: Number, default: 0 },
  max: { type: Number, default: 100 },
  tone: { type: String, default: 'accent' },
  label: { type: String, default: '' },
  suffix: { type: String, default: '' },
})

function pct() {
  if (!props.max) return 0
  return Math.max(0, Math.min(100, (props.value / props.max) * 100))
}
</script>

<template>
  <div class="bar-meter">
    <div class="track">
      <div class="fill" :class="`tone-${tone}`" :style="{ width: pct() + '%' }" />
    </div>
    <div class="meta">
      <span v-if="label" class="label">{{ label }}</span>
      <span class="value mono">{{ value }}{{ suffix }}</span>
    </div>
  </div>
</template>

<style scoped>
.bar-meter {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.track {
  height: 8px;
  background: var(--bg-raised);
  border-radius: 4px;
  overflow: hidden;
}
.fill {
  height: 100%;
  border-radius: 4px;
  transition: width 0.25s ease;
}
.tone-accent { background: var(--accent); }
.tone-accent2 { background: var(--accent-2); }
.tone-ok { background: var(--ok); }
.tone-warn { background: var(--warn); }
.tone-danger { background: var(--danger); }
.tone-muted { background: var(--text-3); }
.meta {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--text-2);
}
.value {
  color: var(--text-1);
}
</style>
