<script setup>
/**
 * 阶段耗时条。数据源为响应中的 timings。
 *
 * 呈现约束：
 *   ① 使用从左到右的填充条，不做分段堆叠；动画只播放一次并停在满格。
 *   ② 每一步做成一个**横向芯片**，按网格铺开（一行 3~4 个、左对齐）——
 *      竖着排时八个阶段要占十几行，把上传区和下面的文件列表撑得很远。
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

/** 填充动画时长：按**标题里那个总耗时**缩放，夹在 0.4~1.6 秒内。
 *  ★ 用 shownTotal（端到端优先）而不是后端埋点总计：动画表现的是一次用户
 *    操作的等待时长，那个数才是用户眼中的“这次花了多久”。 */
const animMs = computed(() => {
  const t = Number(shownTotal.value) || 0
  return Math.max(400, Math.min(1600, Math.round(t * 0.5)))
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
        <span class="fill" :style="{ animationDuration: animMs + 'ms' }">
          <i
            v-for="(s, i) in perf.stages"
            :key="i"
            class="seg"
            :style="{ width: s.pct + '%', background: toneCss[s.tone] }"
            :title="`${s.stage} ${formatMs(s.ms)}`"
          />
        </span>
      </div>

      <!-- 每一步一个芯片：颜色点表示它属于哪一类，右侧是调用次数（×N）。
           按网格铺开，宽度不够时自动换行 —— 不再一路竖着排到底。 -->
      <ol class="stage-rows">
        <li v-for="(s, i) in perf.stages" :key="i" class="row">
          <span class="dot" :style="{ background: toneCss[s.tone] }" />
          <span class="name">{{ s.stage }}</span>
          <span v-if="s.count > 1" class="count mono">×{{ s.count }}</span>
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
/* ---------- 分段彩条：每段宽度 = 该步耗时 ÷ 各步之和，整条从 0 拉到满格 ---------- */
.fill-track {
  position: relative;
  height: 8px;
  border-radius: 4px;
  overflow: hidden;
  background: var(--bg-raised);
}
.fill {
  display: flex;
  height: 100%;
  transform-origin: left center;
  animation-name: fill-grow;
  animation-timing-function: cubic-bezier(.22, .8, .28, 1);
  animation-fill-mode: both;
}
/* 每一段：宽度由耗时占比决定（占比以各段之和为分母，所以刚好填满）。 */
.seg {
  display: block;
  height: 100%;
  flex: none;
  min-width: 1px;
  box-shadow: inset -1px 0 rgba(0, 0, 0, .38);
}
.seg:last-child { box-shadow: none; }
/* 结论为 fail 时只给整条加一道告警描边，不改各段自身的颜色语义。 */
.fill-track.is-danger { box-shadow: inset 0 0 0 1px var(--danger); }
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
/* ---------- 每一步一个芯片，横向铺开 ---------- */
.stage-rows {
  list-style: none;
  margin: 0;
  padding: 0;
  /* 固定 4 列：一行 4 个、逐行左对齐，比「能塞几个塞几个」更好看也更好数。
     窄一点降到 3 列、再窄 2 列 —— 芯片本身不换行，行高始终一致。 */
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 6px 8px;
}
@media (max-width: 1180px) {
  .stage-rows { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 780px) {
  .stage-rows { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
.row {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  padding: 3px 9px;
  font-size: 12px;
  color: var(--text-2);
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--bg-raised);
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 2px;
  flex: none;
  align-self: center;
}
/* 名字过长时用省略号，**不换行** —— 换行会把网格的行高搞成高低不齐。 */
.row .name {
  color: var(--text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.row .count {
  color: var(--text-3);
  margin-left: auto;
  flex: none;
}
</style>
