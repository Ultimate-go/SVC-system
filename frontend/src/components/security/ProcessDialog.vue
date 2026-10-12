<script setup>
/**
 * 过程弹框 —— 把「这件事到底分几步、每步在做什么」一屏讲清楚。
 *
 * ★ 为什么要它（2026-10-12 用户要的）：
 *   上传 / 验证 / 聚合 / 分解这四件事，界面上原来只给一个结果
 *   （「上传成功」「验证通过」）加一条耗时条。对一个要讲密码学的系统来说，
 *   这等于把中间那几步最有说服力的东西藏起来了 —— 而这套系统的卖点恰恰是
 *   「每一步都能拿出来给人看」。
 *
 * ★ 讲故事的三条纪律（用户 2026-10-12 定）：
 *   ① **不展示中间值** —— 不摆 U/C 的十进制、不摆分量、不摆任何密钥材料；
 *      摆的是"做了什么"和**真实**的规模与耗时；
 *   ② 耗时全部来自后端 `timings`，按 `key` 对齐到步骤上；
 *      没有对应 stage 的步骤就留空，不编数字、不四舍五入成一朵花；
 *   ③ 动效只用来说明"进行到哪一步"（当前那一步亮起 + 一道扫光），
 *      并且**都以静止状态收尾**（`prefers-reduced-motion` 下面整个不动）。
 */
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { buildPerf, formatMs } from '../../utils/timing'
import Icon from '../icons/Icon.vue'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  title: { type: String, default: '过程' },
  subtitle: { type: String, default: '' },
  /**
   * [{ key, name, en, desc, estMs, facts }]
   *  - key   与后端 timings 的 stage 名对齐（对不上就不显示耗时）
   *  - desc  这一步在做什么、凭什么可信
   *  - estMs **预估**耗时（一般用“上一次真实值”），决定这一步展示多久
   *  - facts [{ k, v }] 这一步的**真实中间量**（从响应里填进来，没有就不显示）
   */
  steps: { type: Array, default: () => [] },
  /** 后端的 { total_ms, stages:[{stage, ms, count}] }，请求还没回来时为 null。 */
  timings: { type: Object, default: null },
  /** 底部那几个真实参数：[{ k, v }]，v 由调用方保证是真值。 */
  facts: { type: Array, default: () => [] },
  /**
   * ★ 这件事**还在进行中**（请求还没回来）。
   *
   * 用户 2026-10-12 的要求：动画要**跟着实际过程走**，不要“传完了再演”。
   * 所以父组件在**发请求的同时**就把这个框打开（`pending=true`），
   * 动画按预估耗时往下走；走到最后一步时如果请求还没回来，就停在
   * “进行中…”上等它 —— 而不是提前宣布成功或突然关掉。
   */
  pending: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue'])

const close = () => emit('update:modelValue', false)

/** 后端阶段名 → 毫秒（同名合并，×count 取和）。 */
const msByStage = computed(() => {
  const perf = buildPerf(props.timings)
  const out = {}
  for (const s of perf.stages) out[s.stage] = s.ms
  return out
})

const totalMs = computed(() => buildPerf(props.timings).total_ms)
/** 步骤上能显示出来的耗时之和 —— 与 total 不同：总时长还含埋点没覆盖的零碎。 */
const stepMsSum = computed(() =>
  props.steps.reduce((acc, s) => acc + (msByStage.value[s.key] || 0), 0),
)

const current = ref(0)
const playing = ref(false)
/**
 * 每一步展示多久（★ 用户要求：每步至少 0.5 秒；耗时长的那一步也要长一点）。
 *
 * ★ 这里用的是**预估**（上一次的真实值）× 1.15，而不是真实值 —— 因为真实值
 *   要等响应回来才知道，而动画现在**跟请求同时开始**。上一次的真实值是
 *   最靠谱的估计：同一个系统、同一批节点，各阶段的比例很稳定。
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

/** 一步一步往下走（不是等间隔 setInterval —— 每步时长不一样）。 */
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

/** 自动走一遍：走到最后一步就停住（请求还没回来就等着，不自己关）。 */
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

/** 动画走完了、可请求还没回来 —— 停在最后一步显示“进行中”。 */
const waiting = computed(() => props.pending && !playing.value)

/** 当前这一步（详细介绍就展它）。 */
const cur = computed(() => props.steps[current.value] || null)

/** 步骤轨的 DOM —— 用来把**当前步**滚到正中间。 */
const railEl = ref(null)

function centerCurrent() {
  const rail = railEl.value
  if (!rail) return
  const chip = rail.children?.[current.value]
  if (!chip?.scrollIntoView) return
  // 只让**横向**滚到中间（`block: 'nearest'` 避免把整页也一起滚了）
  chip.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' })
}

// ★ 进度条与“当前展示的步骤”同步：每换一步就把那格滚到轨道正中（用户 2026-10-12）。
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
    <!-- 外围循环跑条的彩色装饰（与步骤节奏无关，就一直在跑） -->
    <span class="ring-line" aria-hidden="true" />
    <p v-if="subtitle" class="sub text-2">{{ subtitle }}</p>

    <!-- 横向步骤轨：序号 + 步骤名（+ 英文短名）—— 正在走的那一步放大，其余缩小。
         挤不下就横向滚动，而且**当前步会自己滚到正中间**。 -->
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

    <!-- 进度线：长度 = 走到第几步（与“当前展示的步骤”同步，不是单独一路动画） -->
    <div class="prog" aria-hidden="true">
      <span :style="{ width: ((current + 1) / steps.length) * 100 + '%' }" />
    </div>

    <!-- 当前这一步的详细介绍（一次只展一步，所以再多步也放得下） -->
    <div v-if="cur" class="detail">
      <div class="detail-head">
        <span class="detail-idx mono">{{ current + 1 }}/{{ steps.length }}</span>
        <span class="detail-name">{{ cur.name }}</span>
        <span v-if="cur.en" class="en mono">{{ cur.en }}</span>
        <span v-if="waiting && current === steps.length - 1" class="running mono">进行中…</span>
        <span v-if="msByStage[cur.key] !== undefined" class="ms mono">{{ formatMs(msByStage[cur.key]) }}</span>
      </div>
      <p class="desc">{{ cur.desc }}</p>
      <!-- 这一步的**真实中间量**（从响应里填；没有就不显示） -->
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
      这一步的总耗时 {{ formatMs(totalMs) }}<template v-if="stepMsSum"> · 上面各步合计 {{ formatMs(stepMsSum) }}</template>
    </p>

    <template #footer>
      <div class="foot">
        <!-- ★ 首步不显示「上一步」、末步不显示「下一步」；中间步两个都有
             （用户 2026-10-12：别放一个点不动的按钮在那里）。 -->
        <el-button v-if="current > 0" @click="go(current - 1)">上一步</el-button>
        <el-button link @click="playing ? stop() : play()">
          <Icon :name="playing ? 'x' : 'pulse'" :size="13" style="margin-right: 5px" />
          {{ playing ? '停在当前' : '重看一遍' }}
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
.steps { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 8px; }
.step {
  position: relative;
  display: flex;
  gap: 11px;
  padding: 10px 12px;
  border: 1px solid transparent;
  border-radius: 9px;
  background: var(--bg-raised);
  overflow: hidden;
  transition: opacity .2s ease, border-color .2s ease, background .2s ease;
}
.step.is-wait { opacity: .42; }
.step.is-now {
  border-color: color-mix(in srgb, var(--accent) 38%, transparent);
  background: color-mix(in srgb, var(--accent) 6%, var(--bg-raised));
}
.mark { width: 18px; flex: none; display: inline-flex; justify-content: center; padding-top: 1px; }
.dot-done { color: var(--ok); font-size: 14px; line-height: 1; }
.dot-idx {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  font-size: 11px;
  color: var(--text-2);
  border: 1px solid var(--line);
}
.step.is-now .dot-idx { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 45%, transparent); }
.body { min-width: 0; flex: 1; }
.head { display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap; }
.name { color: var(--text-1); font-size: 13px; }
.en { color: var(--text-3); font-size: 10px; letter-spacing: .08em; }
.ms { margin-left: auto; color: var(--text-2); font-size: 11px; }
.desc { margin: 5px 0 0; color: var(--text-2); font-size: 12px; line-height: 1.7; }
/* 真实中间量：一行一个 k/v，值用等宽体（好看清位数） */
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
  font-size: 11px;
  line-height: 1.6;
}
.s-facts .fk { flex: none; min-width: 92px; }
.s-facts .fv { color: var(--text-1); word-break: break-all; }
.running { color: var(--accent); font-size: 10px; letter-spacing: .08em; }
/* ---------- 横向步骤轨（★ 用户 2026-10-12） ---------- */
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
/* 未走到的：缩小、变淡，一眼能看出“还没轮到” */
.rail-item.is-wait { opacity: .45; transform: scale(.9); }
.rail-item.is-done { border-color: color-mix(in srgb, var(--ok) 34%, transparent); }
.rail-item.is-now {
  transform: scale(1.12);
  border-color: color-mix(in srgb, var(--accent) 55%, transparent);
  background: color-mix(in srgb, var(--accent) 9%, var(--bg-raised));
  color: var(--text-1);
}
.rail-idx { color: var(--accent); font-size: 11px; }
.rail-name { font-size: 11px; }
.rail-item.is-now .rail-name { font-size: 13px; font-weight: 500; }
.rail-en { font-size: 9px; color: var(--text-3); letter-spacing: .06em; }
.rail-item.is-now .rail-en { color: var(--accent); }
/* ---------- 当前步的详细介绍 ---------- */
.detail {
  margin-top: 10px;
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--bg-raised);
  min-height: 104px;
}
.detail-head { display: flex; align-items: baseline; gap: 9px; flex-wrap: wrap; }
.detail-idx { color: var(--text-3); font-size: 11px; }
.detail-name { color: var(--text-1); font-size: 15px; font-weight: 500; }
/* ---------- 循环跑条的彩色装饰（用户 2026-10-12；与步骤节奏无关） ---------- */
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
/* 弹框边缘的呼吸外光也算装饰的一部分（颜色缓慢循环） */
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
/* 进度线：长度 = 走到第几步 */
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
/* 扫光只说明“正在这一步”，以静止状态收尾（100% 停住）。 */
.scanline {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  width: 40%;
  background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--accent) 15%, transparent), transparent);
  animation: scan 1.1s linear infinite;
  pointer-events: none;
}
@keyframes scan {
  from { transform: translateX(-110%); }
  to { transform: translateX(310%); }
}
@media (prefers-reduced-motion: reduce) {
  .scanline { display: none; }
}
.facts { display: flex; flex-wrap: wrap; gap: 6px 20px; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--line); }
.fact { display: inline-flex; gap: 6px; align-items: baseline; font-size: 12px; }
.fact .v { color: var(--text-1); }
.total { margin: 10px 0 0; font-size: 11px; }
.foot { display: flex; align-items: center; gap: 6px; }
.spacer { flex: 1; }
</style>
