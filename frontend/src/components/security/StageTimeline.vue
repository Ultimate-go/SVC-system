<script setup>
/**
 * 阶段耗时条 —— 数据源是响应里的 timings。
 *
 * 把后端的 { total_ms, stages: [{stage, ms, count}] } 画成一条横向堆叠条，
 * 每段宽度按「各段之和」的比例定，不按 total（total 会略大，差额是埋点没覆盖的零碎）。
 *
 * ★ timings 不编百分比：这里只画后端真实返回的毫秒数。
 */
import { computed } from 'vue'
import { buildPerf, formatMs } from '../../utils/timing'

const props = defineProps({
  timings: { type: Object, default: null },
})

const perf = computed(() => buildPerf(props.timings))

const toneCss = {
  accent: 'var(--accent)',
  accent2: 'var(--accent-2)',
  ok: 'var(--ok)',
  warn: 'var(--warn)',
  danger: 'var(--danger)',
  muted: 'var(--text-3)',
}
</script>

<template>
  <div class="stage-timeline" v-if="perf.stages.length">
    <div class="bar">
      <div
        v-for="(s, i) in perf.stages"
        :key="i"
        class="seg"
        :style="{ width: s.pct + '%', background: toneCss[s.tone] }"
        :title="`${s.stage}：${formatMs(s.ms)}${s.count > 1 ? ' ×' + s.count : ''}`"
      />
    </div>
    <div class="legend">
      <span class="total mono">合计 {{ formatMs(perf.total_ms) }}</span>
      <span
        v-for="(s, i) in perf.stages"
        :key="i"
        class="item"
        :style="{ color: toneCss[s.tone] }"
      >
        <span class="chip" :style="{ background: toneCss[s.tone] }" />
        {{ s.stage }} {{ formatMs(s.ms) }}<template v-if="s.count > 1"> ×{{ s.count }}</template>
      </span>
    </div>
  </div>
</template>

<style scoped>
.stage-timeline {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bar {
  display: flex;
  height: 10px;
  border-radius: 5px;
  overflow: hidden;
  background: var(--bg-raised);
}
.seg {
  height: 100%;
  min-width: 2px;
  transition: width 0.25s ease;
}
.legend {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  font-size: 12px;
  color: var(--text-2);
}
.total {
  color: var(--text-1);
}
.item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.chip {
  width: 8px;
  height: 8px;
  border-radius: 2px;
  display: inline-block;
}
</style>
