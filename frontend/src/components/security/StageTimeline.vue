<script setup>
/**
 * 阶段耗时条 —— 数据源是响应里的 timings。
 *
 * ★ 2026-10-12 改版（用户要求）：
 *   ① 不再画「按比例堆叠的多色条」，改成**一道从左到右填满的条**
 *      （与登录页“正在派生口令”同一个观感）；动画只走一次、**停在满格**，
 *      不做循环的假进度；
 *   ② 每一步**单独一行**显示它自己的耗时（原来全挤在一条 legend 里），
 *      颜色仍用原先那套 tone，一眼能看出“时间花在哪一步”；
 *   ③ `state` 为 `fail` 时那道条用告警色 —— 验证不通过时不能长得跟通过一样。
 *
 * ★ timings 不编百分比：这里只画后端真实返回的毫秒数。
 */
import { computed } from 'vue'
import { buildPerf, formatMs } from '../../utils/timing'

const props = defineProps({
  timings: { type: Object, default: null },
  /** 结论态：''（中性）/ 'ok' / 'fail' —— 只影响那道填满条的颜色。 */
  state: { type: String, default: '' },
})

const perf = computed(() => buildPerf(props.timings))

/** 填满动画的时长：按真实总耗时缩一下，但夹在 0.5~1.6 秒之间（太快看不见、太慢碍事）。 */
const animMs = computed(() => {
  const t = Number(perf.value.total_ms) || 0
  return Math.max(500, Math.min(1600, Math.round(t * 0.6)))
})

const fillTone = computed(() =>
  props.state === 'fail' ? 'danger' : props.state === 'ok' ? 'ok' : 'accent',
)

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
  <div class="stage-fill" v-if="perf.stages.length">
    <div class="fill-track" :class="`is-${fillTone}`">
      <span class="fill" :style="{ animationDuration: animMs + 'ms' }" />
    </div>
    <div class="fill-total mono">合计 {{ formatMs(perf.total_ms) }}</div>

    <!-- 每一步单独一行：阶段名 + 它自己的耗时（不再挤成一长条） -->
    <ol class="stage-rows">
      <li v-for="(s, i) in perf.stages" :key="i" class="row">
        <span class="dot" :style="{ background: toneCss[s.tone] }" />
        <span class="name">{{ s.stage }}</span>
        <span v-if="s.count > 1" class="count mono">×{{ s.count }}</span>
        <span class="ms mono">{{ formatMs(s.ms) }}</span>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.stage-fill {
  display: flex;
  flex-direction: column;
  gap: 9px;
}
/* ---------- 一道从左到右填满的条（动画结束停在满格） ---------- */
.fill-track {
  position: relative;
  height: 6px;
  border-radius: 3px;
  overflow: hidden;
  background: var(--bg-raised);
}
.fill {
  display: block;
  height: 100%;
  transform-origin: left center;
  animation-name: fill-grow;
  animation-timing-function: cubic-bezier(.22, .8, .28, 1);
  animation-fill-mode: both;
}
.fill-track.is-accent .fill { background: linear-gradient(90deg, var(--accent), var(--accent-2)); }
.fill-track.is-ok .fill { background: linear-gradient(90deg, color-mix(in srgb, var(--ok) 70%, var(--accent)), var(--ok)); }
.fill-track.is-danger .fill { background: linear-gradient(90deg, color-mix(in srgb, var(--danger) 65%, var(--warn)), var(--danger)); }
.fill-track::after {
  /* 一道扫光只说明“正在长”，随填充一起停住 */
  content: '';
  position: absolute;
  inset: 0;
  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, .28), transparent);
  width: 35%;
  transform: translateX(-120%);
  animation: fill-sheen 1.05s ease-out both;
  pointer-events: none;
}
@keyframes fill-grow {
  from { transform: scaleX(0); }
  to { transform: scaleX(1); }
}
@keyframes fill-sheen {
  from { transform: translateX(-120%); }
  to { transform: translateX(320%); }
}
@media (prefers-reduced-motion: reduce) {
  .fill { animation: none; transform: scaleX(1); }
  .fill-track::after { display: none; }
}
.fill-total {
  color: var(--text-1);
  font-size: 12px;
}
/* ---------- 每一步单独一行 ---------- */
.stage-rows {
  list-style: none;
  margin: 2px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.row {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
  color: var(--text-2);
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 2px;
  flex: none;
  align-self: center;
}
.row .name { color: var(--text-2); }
.row .count { color: var(--text-3); }
.row .ms { margin-left: auto; color: var(--text-1); white-space: nowrap; }
</style>
