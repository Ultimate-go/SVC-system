<script setup>
/**
 * 过程弹框：展示一次操作的步骤序列、各步耗时与规模参数。
 *
 * 展示约束（不变式）：
 *   ① 不展示中间值。不输出 U/C 的十进制、不输出分量、不输出任何密钥材料；
 *      只输出步骤名、规模（块数 / 位置数 / 文件数）与耗时。
 *   ② 耗时只取自后端 `timings`，按 `key` 与步骤对齐。
 *      无对应 stage 的步骤留空；不估算、不补齐。
 *   ③ 动效仅用于指示当前步骤。所有动画以静止状态收尾；
 *      `prefers-reduced-motion` 生效时全部禁用。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { buildPerf, formatMs } from '../../utils/timing'
import Icon from '../icons/Icon.vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, default: '处理过程' },
  subtitle: { type: String, default: '' },
  /**
   * 步骤数组：[{ key, name, en, desc, estMs, facts }]
   *  - key    与后端 timings 的 stage 名一致；不一致时该步不显示耗时
   *  - name   步骤名（界面显示）
   *  - en     英文短名（界面显示，可省略）
   *  - desc   该步做什么、判据是什么
   *  - estMs  预估耗时（取上一次的实际值），决定该步的展示时长
   *  - facts  [{ k, v }] 该步的中间量；缺省时不显示
   */
  steps: { type: Array, default: () => [] },
  /** 后端返回的 { total_ms, stages: [{ stage, ms, count }] }；尚未返回时为 null。 */
  timings: { type: Object, default: null },
  /** 底部参数列表 [{ k, v }]；取值由调用方保证为真值。 */
  facts: { type: Array, default: () => [] },
  /**
   * 请求是否仍在进行中。
   *
   * 不变式：父组件在发起请求的同时以 `pending = true` 打开本框，动画按预估
   * 耗时推进；推进到最后一步而请求未返回时停在最后一步并显示进行中状态，
   * 不得提前显示完成，也不得自动关闭。
   */
  pending: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue'])

const close = () => emit('update:modelValue', false)

/** 后端阶段名到毫秒的映射（同名合并，按 count 求和）。 */
const msByStage = computed(() => {
  const perf = buildPerf(props.timings)
  const out = {}
  for (const s of perf.stages) out[s.stage] = s.ms
  return out
})

const totalMs = computed(() => buildPerf(props.timings).total_ms)
/** 各步可显示耗时之和。与 total_ms 的差额为埋点未覆盖的阶段。 */
const stepMsSum = computed(() =>
  props.steps.reduce((acc, s) => acc + (msByStage.value[s.key] || 0), 0),
)

const current = ref(0)
const playing = ref(false)
/**
 * 每一步的展示时长。
 *
 * 取值：estMs（上一次的实际值）× 1.15，并夹在 [500, 2600] ms 内。
 * 使用预估值而非实际值的原因：动画与请求同时开始，此时实际值不可知；
 * 同一系统与同一批节点下各阶段耗时的比例稳定，故上一次的实际值可用。
 */
const MIN_STEP_MS = 500
const MAX_STEP_MS = 2600

function dwellOf(i) {
  const est = Number(props.steps[i]?.estMs) || 0
  return Math.max(MIN_STEP_MS, Math.min(MAX_STEP_MS, Math.round(est * 1.15)))
}

let timer

function stop() {
  if (timer !== undefined) {
    window.clearTimeout(timer)
    timer = undefined
  }
  playing.value = false
}

function reducedMotion() {
  return window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true
}

function go(i) {
  stop()
  current.value = Math.max(0, Math.min(props.steps.length - 1, i))
}

function next() {
  if (current.value >= props.steps.length - 1) {
    stop()
    return
  }
  go(current.value + 1)
}

/** 依次推进。各步时长不等，故不能用等间隔 setInterval。 */
function walk() {
  if (current.value >= props.steps.length - 1) {
    playing.value = false
    return
  }
  timer = window.setTimeout(() => {
    current.value += 1
    walk()
  }, dwellOf(current.value))
}

/** 从头自动播放。到达最后一步后停止；请求未返回时停留在该步，不自动关闭。 */
function play() {
  stop()
  if (reducedMotion()) {
    current.value = props.steps.length - 1
    return
  }
  current.value = 0
  playing.value = true
  walk()
}

/** 动画已到达最后一步，而请求尚未返回。 */
const waiting = computed(() => props.pending && !playing.value)

/** 当前步骤对象。 */
const cur = computed(() => props.steps[current.value] || null)

/** 步骤轨容器，用于把当前步滚动到可视区中央。 */
const railEl = ref(null)

function centerCurrent() {
  const rail = railEl.value
  if (!rail) return
  const chip = rail.children?.[current.value]
  if (!chip?.scrollIntoView) return
  // 仅横向滚动：block: 'nearest' 避免连带滚动整页。
  chip.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' })
}

// 换步时把当前步滚动到轨道中央，使进度线与当前步一致。
watch(current, () => nextTick(centerCurrent))

watch(
  () => props.modelValue,
  (open) => {
    if (open) play()
    else stop()
  },
)

onBeforeUnmount(stop)
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="min(820px, 94vw)"
    class="process-dialog"
    @update:model-value="emit('update:modelValue', $event)"
    @closed="stop"
  >
    <!-- 装饰彩条。与步骤进度无关，持续循环播放。 -->
    <span class="ring-line" aria-hidden="true" />
    <p v-if="subtitle" class="sub text-2">{{ subtitle }}</p>

    <!-- 步骤轨：序号 + 步骤名 + 英文短名。当前步放大，其余缩小；
         溢出时横向滚动，当前步保持居中。 -->
    <div ref="railEl" class="rail" role="tablist">
      <button
        v-for="(s, i) in steps"
        :key="s.key || i"
        type="button"
        class="rail-item"
        :class="i < current ? 'is-done' : i === current ? 'is-now' : 'is-wait'"
        :title="s.name"
        @click="go(i)"
      >
        <span class="rail-idx mono">{{ i < current ? '✓' : i + 1 }}</span>
        <span class="rail-name">{{ s.name }}</span>
        <span v-if="s.en" class="rail-en mono">{{ s.en }}</span>
      </button>
    </div>

    <!-- 进度线。宽度 = (当前步序号 + 1) / 总步数，与当前步同步。 -->
    <div class="prog" aria-hidden="true">
      <span :style="{ width: ((current + 1) / steps.length) * 100 + '%' }" />
    </div>

    <!-- 当前步的说明。一次只显示一步。 -->
    <div v-if="cur" class="detail">
      <div class="detail-head">
        <span class="detail-idx mono">{{ current + 1 }}/{{ steps.length }}</span>
        <span class="detail-name">{{ cur.name }}</span>
        <span v-if="cur.en" class="en mono">{{ cur.en }}</span>
        <span v-if="waiting && current === steps.length - 1" class="running mono">进行中</span>
        <span v-if="msByStage[cur.key] !== undefined" class="ms mono">{{ formatMs(msByStage[cur.key]) }}</span>
      </div>
      <p class="desc">{{ cur.desc }}</p>
      <!-- 该步的中间量；无数据时不显示。 -->
      <ul v-if="cur.facts?.length" class="s-facts">
        <li v-for="f in cur.facts" :key="f.k">
          <span class="fk text-3">{{ f.k }}</span>
          <span class="fv mono">{{ f.v }}</span>
        </li>
      </ul>
    </div>

    <div v-if="facts.length" class="facts">
      <span v-for="f in facts" :key="f.k" class="fact">
        <span class="k text-3">{{ f.k }}</span><span class="v mono">{{ f.v }}</span>
      </span>
    </div>

    <p v-if="timings" class="total mono text-3">
      总耗时 {{ formatMs(totalMs) }}<template v-if="stepMsSum"> · 各步合计 {{ formatMs(stepMsSum) }}</template>
    </p>

    <template #footer>
      <div class="foot">
        <!-- 首步不显示「上一步」，末步不显示「下一步」；中间步两者都有。 -->
        <el-button v-if="current > 0" @click="go(current - 1)">上一步</el-button>
        <el-button link @click="playing ? stop() : play()">
          <Icon :name="playing ? 'x' : 'pulse'" :size="13" style="margin-right: 5px" />
          {{ playing ? '暂停' : '重新播放' }}
        </el-button>
        <span class="spacer" />
        <el-button @click="close">关闭</el-button>
        <el-button v-if="current < steps.length - 1" type="primary" @click="next">下一步</el-button>
      </div>
    </template>
    <span class="ring-line ring-bottom" aria-hidden="true" />
  </el-dialog>
</template>

<style scoped>
.sub { margin: 0 0 14px; font-size: 13px; }
.en { color: var(--text-3); font-size: 13px; letter-spacing: .08em; }
.ms { margin-left: auto; color: var(--text-2); font-size: 13px; }
.desc { margin: 5px 0 0; color: var(--text-2); font-size: 13px; line-height: 1.7; }
/* 中间量列表：每行一个 k/v，数值用等宽字体。 */
.s-facts {
  list-style: none;
  margin: 6px 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.s-facts li {
  display: flex;
  gap: 8px;
  align-items: baseline;
  font-size: 13px;
  line-height: 1.6;
}
.s-facts .fk { flex: none; min-width: 92px; }
.s-facts .fv { color: var(--text-1); word-break: break-all; }
.running { color: var(--accent); font-size: 13px; letter-spacing: .08em; }
/* ---------- 步骤轨 ---------- */
.rail {
  display: flex;
  align-items: flex-end;
  gap: 6px;
  overflow-x: auto;
  padding: 2px 0 6px;
}
.rail-item {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 9px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--bg-raised);
  color: var(--text-2);
  cursor: pointer;
  transition: transform .18s ease, opacity .18s ease, border-color .18s ease, background .18s ease;
}
/* 未到达的步骤：缩小并降低不透明度。 */
.rail-item.is-wait { opacity: .45; transform: scale(.9); }
.rail-item.is-done { border-color: color-mix(in srgb, var(--ok) 34%, transparent); }
.rail-item.is-now {
  transform: scale(1.12);
  border-color: color-mix(in srgb, var(--accent) 55%, transparent);
  background: color-mix(in srgb, var(--accent) 9%, var(--bg-raised));
  color: var(--text-1);
}
.rail-idx { color: var(--accent); font-size: 13px; }
.rail-name { font-size: 13px; }
.rail-item.is-now .rail-name { font-size: 13px; font-weight: 500; }
.rail-en { font-size: 13px; color: var(--text-3); letter-spacing: .06em; }
.rail-item.is-now .rail-en { color: var(--accent); }
/* ---------- 当前步说明 ---------- */
.detail {
  margin-top: 10px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--bg-raised);
  min-height: 104px;
}
.detail-head { display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap; }
.detail-idx { color: var(--text-3); font-size: 13px; }
.detail-name { color: var(--text-1); font-size: 15px; font-weight: 500; }
/* ---------- 装饰彩条：循环播放，与步骤进度无关 ---------- */
.ring-line {
  position: relative;
  display: block;
  height: 2px;
  margin: -6px 0 12px;
  border-radius: 2px;
  overflow: hidden;
  background: var(--bg-raised);
}
.ring-line.ring-bottom { margin: -6px 0 0; }
.ring-line::after {
  content: '';
  position: absolute;
  top: 0;
  bottom: 0;
  width: 32%;
  background: linear-gradient(90deg, transparent, var(--accent), var(--accent-2), var(--ok), transparent);
  animation: ring-run 2.8s linear infinite;
}
@keyframes ring-run {
  from { transform: translateX(-110%); }
  to { transform: translateX(420%); }
}
/* 对话框边缘光晕。同为装饰，颜色缓慢循环。 */
:deep(.el-dialog.process-dialog) {
  animation: edge-glow 6s ease-in-out infinite;
}
@keyframes edge-glow {
  0%, 100% { box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent) 26%, transparent), 0 20px 60px rgba(0, 0, 0, .45); }
  33% { box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent-2) 32%, transparent), 0 20px 60px rgba(0, 0, 0, .45); }
  66% { box-shadow: 0 0 0 1px color-mix(in srgb, var(--ok) 28%, transparent), 0 20px 60px rgba(0, 0, 0, .45); }
}
@media (prefers-reduced-motion: reduce) {
  .ring-line::after { animation: none; transform: translateX(180%); }
  :deep(.el-dialog.process-dialog) { animation: none; }
}
/* 进度线：宽度对应当前步序号。 */
.prog {
  height: 3px;
  margin: 2px 0 12px;
  border-radius: 2px;
  background: var(--bg-raised);
  overflow: hidden;
}
.prog span {
  display: block;
  height: 100%;
  border-radius: 2px;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
  transition: width .28s ease;
}
.facts { display: flex; flex-wrap: wrap; gap: 6px 20px; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
.fact { display: inline-flex; gap: 6px; align-items: baseline; font-size: 13px; }
.fact .v { color: var(--text-1); }
.total { margin: 10px 0 0; font-size: 13px; }
.foot { display: flex; align-items: center; gap: 6px; }
.spacer { flex: 1; }
</style>
