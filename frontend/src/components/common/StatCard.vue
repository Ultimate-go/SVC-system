<script setup>
/**
 * 指标卡：顶部 2px 的 accent→accent-2 线性条，下面大字数值 + 小字单位，
 * 再下面一行 12px 说明（数据来源）。
 */
defineProps({
  label: { type: String, required: true },
  value: { type: [String, Number], default: '' },
  unit: { type: String, default: '' },
  hint: { type: String, default: '' },
  mono: { type: Boolean, default: true },
})
</script>

<template>
  <div class="stat-card">
    <div class="bar" />
    <div class="label">{{ label }}</div>
    <div class="value" :class="{ mono }">
      {{ value }}<span class="unit" v-if="unit">{{ unit }}</span>
    </div>
    <div class="hint" v-if="hint">{{ hint }}</div>
  </div>
</template>

<style scoped>
.stat-card {
  background: var(--bg-panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: inset 0 1px 0 var(--glow-inset);
  padding: 14px 16px;
  position: relative;
  overflow: hidden;
}
.bar {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
}
.label {
  font-size: 12px;
  color: var(--text-2);
}
.value {
  font-size: 28px;
  font-weight: 500;
  line-height: 1.2;
  margin: 4px 0 2px;
  color: var(--text-1);
}
.unit {
  font-size: 13px;
  font-weight: 400;
  color: var(--text-2);
  margin-left: 4px;
}
.hint {
  font-size: 12px;
  color: var(--text-3);
}
</style>
