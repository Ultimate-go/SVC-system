<script setup>
/**
 * 内联 SVG 折线 —— polyline，用于趋势。
 * props: points(number[]), width, height, tone。
 */
import { computed } from 'vue'

const props = defineProps({
  points: { type: Array, default: () => [] },
  width: { type: Number, default: 240 },
  height: { type: Number, default: 80 },
  tone: { type: String, default: 'accent' },
})

const color = computed(() => {
  const map = {
    accent: 'var(--accent)',
    accent2: 'var(--accent-2)',
    ok: 'var(--ok)',
    warn: 'var(--warn)',
    danger: 'var(--danger)',
  }
  return map[props.tone] || map.accent
})

const polyPoints = computed(() => {
  const pts = props.points.filter((p) => typeof p === 'number')
  if (pts.length === 0) return ''
  const max = Math.max(...pts, 1)
  const min = Math.min(...pts, 0)
  const range = max - min || 1
  const pad = 4
  const stepX = (props.width - pad * 2) / (pts.length - 1 || 1)
  return pts
    .map((v, i) => {
      const x = pad + i * stepX
      const y = props.height - pad - ((v - min) / range) * (props.height - pad * 2)
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
})
</script>

<template>
  <svg :width="width" :height="height" class="sparkline">
    <polyline :points="polyPoints" fill="none" :stroke="color" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round" />
  </svg>
</template>

<style scoped>
.sparkline {
  display: block;
}
</style>
