<script setup>
/**
 * 内联 SVG 图标集。全部自己写 <svg>，不引外部字体或 sprite。
 *
 * 用法只有一种：<Icon name="dashboard" :size="14" />（图标名见下面的 ICONS 表）。
 * 图标表通过 defineExpose 暴露，方便别处读名字，但**没有** IconDashboard 这类
 * 具名导出 —— 别去 import 它们。
 */
import { computed } from 'vue'

const ICONS = {
  dashboard: 'M4 4h6v16H4zM14 4h6v7h-6zM14 15h6v5h-6z',
  user: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm-8 8a8 8 0 0 1 16 0',
  users: 'M9 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm-6 8a6 6 0 0 1 12 0M17 11a3 3 0 1 0 0-6M15 19a6 6 0 0 1 6-6',
  server: 'M4 4h16v7H4zM4 13h16v7H4zM7 7h.01M7 16h.01',
  file: 'M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9zM14 3v6h6',
  shield: 'M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z',
  activity: 'M3 12h4l2-7 4 14 2-7h6',
  chart: 'M4 20V4M4 20h16M8 16v-4M12 16V8M16 16v-6',
  list: 'M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01',
  plus: 'M12 5v14M5 12h14',
  search: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16zM21 21l-4.35-4.35',
  refresh: 'M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6',
  logout: 'M16 17l5-5-5-5M21 12H9M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4',
  lock: 'M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4',
  unlock: 'M5 11h14v10H5zM8 11V7a4 4 0 0 1 7.7-1.5',
  eye: 'M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z',
  check: 'M4 12l5 5L20 6',
  x: 'M6 6l12 12M18 6L6 18',
  warn: 'M12 3l10 18H2zM12 9v5M12 17h.01',
  info: 'M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 11v6M12 7h.01',
  key: 'M15 7a5 5 0 1 0-5.8 5.8L8 14l-1 1-1-1-1 1v2h2l2-2 1.2 1.2A5 5 0 0 0 15 7zM15 9h.01',
  back: 'M15 18l-6-6 6-6',
  copy: 'M8 8h12v12H8zM4 16V4h12',
  upload: 'M12 16V4M7 9l5-5 5 5M4 20h16',
  download: 'M12 4v12M7 11l5 5 5-5M4 20h16',
  pulse: 'M2 12h3l2-6 4 12 2-6h3',
}

const props = defineProps({
  name: { type: String, required: true },
  size: { type: [Number, String], default: 16 },
  strokeWidth: { type: [Number, String], default: 1.6 },
})

const d = computed(() => ICONS[props.name] || ICONS.info)

defineExpose({ ICONS })
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="strokeWidth"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
  >
    <path :d="d" />
  </svg>
</template>
