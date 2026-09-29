<script setup>
/**
 * SVG 环形仪表 —— stroke-dasharray。用于「已用位置 / n_max」。
 */
import { computed } from 'vue'

const props = defineProps({
  value: { type: Number, default: 0 },
  max: { type: Number, default: 100 },
  size: { type: Number, default: 120 },
  stroke: { type: Number, default: 10 },
  label: { type: String, default: '' },
})

const r = computed(() => (props.size - props.stroke) / 2)
const c = computed(() => 2 * Math.PI * r.value)
const ratio = computed(() => (props.max > 0 ? Math.min(1, props.value / props.max) : 0))
const dash = computed(() => `${c.value * ratio.value} ${c.value}`)
const tone = computed(() => {
  if (ratio.value >= 0.9) return 'var(--warn)'
  return 'var(--accent)'
})
</script>

<template>
  <div class="ring-gauge" :style="{ width: size + 'px', height: size + 'px' }">
    <svg :width="size" :height="size">
      <circle
        :cx="size / 2"
        :cy="size / 2"
        :r="r"
        fill="none"
        stroke="var(--bg-raised)"
        :stroke-width="stroke"
      />
      <circle
        :cx="size / 2"
        :cy="size / 2"
        :r="r"
        fill="none"
        :stroke="tone"
        :stroke-width="stroke"
        stroke-linecap="round"
        :stroke-dasharray="dash"
        :style="{ transform: `rotate(-90deg)`, transformOrigin: 'center' }"
      />
    </svg>
    <div class="center">
      <div class="val mono">{{ value }}<span class="den">/{{ max }}</span></div>
      <div class="lbl" v-if="label">{{ label }}</div>
    </div>
  </div>
</template>

<style scoped>
.ring-gauge {
  position: relative;
}
.center {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}
.val {
  font-size: 20px;
  font-weight: 500;
  color: var(--text-1);
}
.den {
  font-size: 12px;
  color: var(--text-3);
}
.lbl {
  font-size: 11px;
  color: var(--text-2);
}
</style>
