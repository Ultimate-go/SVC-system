<script setup>
/**
 * 阶段耗时条。数据源为响应中的 timings。
 *
 * 呈现约束：
 *   ① 使用从左到右的填充条，不做分段堆叠；动画只播放一次并停在满格。
 *   ② 每一步单独一行显示该步耗时，颜色沿用 tone 区分。
 *   ③ `state` 为 `fail` 时填充条使用告警色。
 *
 * 数据约束：只绘制后端返回的毫秒值，不换算百分比。
 */
import { computed } from 'vue'
import { buildPerf, formatMs } from '../../utils/timing'

const props = defineProps({
  timings: { type: Object, default: null },
  /**
   * 端到端耗时（毫秒）：从用户点击到结果返回的墙钟时间，由调用方计时后传入。
   *
   * ★ 传了就用它 —— 同一个操作在界面上只允许有一个“总耗時”数字：
   *   上传按钮旁写“已用 4.9 s”，这里就不能写另一个数。
   * ★ 不传（如详情页写操作、改口令）：退回后端埋点的 total_ms，标题写「合计」，
   *   含义是各阶段埋点之和（与下方逐行相加可以对照）。
   */
  totalMs: { type: Number, default: null },
  /** 结论态：''（中性）/ 'ok' / 'fail'；只影响填充条颜色。 */
  state: { type: String, default: '' },
})

const perf = computed(() => buildPerf(props.timings))

/** 各阶段埋点之和（只用于悬浮说明，不影响标题里显示的那个数）。 */
const stepSum = computed(() => perf.value.stages.reduce((a, s) => a + (Number(s.ms) || 0), 0))

const hasTotal = computed(() => typeof props.totalMs === 'number' && props.totalMs > 0)
/** 标题里那个数：优先端到端墙钟，退路是后端 total_ms。 */
const shownTotal = computed(() => (hasTotal.value ? props.totalMs : perf.value.total_ms))
const totalLabel = computed(() => (hasTotal.value ? '总耗时' : '合计'))
/** 悬浮说明：把三个口径都写清楚，避免“这个数怎么对不上”的疑问。 */
const metaHint = computed(() => {
  const parts = [`各步合计 ${formatMs(stepSum.value)}`, `后端埋点总计 ${formatMs(perf.value.total_ms)}`]
  if (hasTotal.value) parts.unshift(`端到端（含浏览器端加解密与网络往返）${formatMs(props.totalMs)}`)
  return parts.join(' · ')
})

/** 填充动画时长：按实际总耗时缩放，夹在 0.5~1.6 秒内。 */
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
  <!-- 默认展开；点标题行可收起，左侧小三角随之旋转。 -->
  <details v-if="perf.stages.length" class="stage-fill" open>
    <summary class="fill-summary">
      <span class="fill-tri" aria-hidden="true">▶</span>
      <span class="fill-label">步骤耗时</span>
      <span class="fill-meta mono" :title="metaHint">{{ perf.stages.length }} 步 · {{ totalLabel }} {{ formatMs(shownTotal) }}</span>
    </summary>

    <div class="fill-body">
      <div class="fill-track" :class="`is-${fillTone}`">
        <span class="fill" :style="{ animationDuration: animMs + 'ms' }" />
      </div>

      <!-- 每一步单独一行：阶段名与该步耗时。 -->
      <ol class="stage-rows">
        <li v-for="(s, i) in perf.stages" :key="i" class="row">
          <span class="dot" :style="{ background: toneCss[s.tone] }" />
          <span class="name">{{ s.stage }}</span>
          <span v-if="s.count > 1" class="count mono">×{{ s.count }}</span>
          <span class="ms mono">{{ formatMs(s.ms) }}</span>
        </li>
      </ol>
    </div>
  </details>
</template>

<style scoped>
.stage-fill {
  display: block;
}
/* ---------- 标题行：左侧小三角 + 步数与合计 ---------- */
.fill-summary {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--text-2);
  cursor: pointer;
  list-style: none;
  user-select: none;
}
.fill-summary::-webkit-details-marker { display: none; }
.fill-tri {
  font-size: 10px;
  line-height: 1;
  color: var(--text-3);
  transition: transform .18s ease;
}
details[open] > .fill-summary .fill-tri { transform: rotate(90deg); }
.fill-meta { margin-left: auto; color: var(--text-3); }
.fill-body {
  display: flex;
  flex-direction: column;
  gap: 9px;
  margin-top: 9px;
}
/* ---------- 填充条：动画结束停在满格 ---------- */
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
  /* 扫光用于指示填充进行中，随填充一同停止。 */
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
  font-size: 13px;
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
  font-size: 13px;
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
