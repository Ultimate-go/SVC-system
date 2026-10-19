<script setup>
/**
 * 初始化流水线 —— 把「这套系统的参数是怎么定下来的」画成一条**可以播放**的流水线。
 *
 * ★ 与总览页那六张链路卡**刻意不同**（同一屏里不能看起来是一套东西）：
 *
 *   | | 总览页链路卡 | 这一条初始化流水线 |
 *   |---|---|---|
 *   | 形态 | 横向一排彩色方卡 + 箭头 | 纵向的**部署清单**，一张卡一段 |
 *   | 内容 | 阶段名 + 一句"在做什么" | 阶段名 + `参数名 = 值` 的**键值对** |
 *   | 交互 | 可点开看密码学原理（多页签） | 不可点，只随播放依次点亮 |
 *   | 配色 | cyan/blue/violet/green/amber/rose 六色 | **单色**（accent）+ 等宽值 |
 *   | 数据动效 | 数据包沿横向轨道传递 | 数据包沿纵向连线**逐段下流**，卡片逐张点亮 |
 *
 * 用法：父组件把 `steps` 填好，然后调 `play()`（依次点亮并**展开**每张卡）；
 * 已经演过一遍时（同一次登录内再进来）调 `finish()` 直接呈现完成态。
 * `steps` 里每张卡：``{ key, no, title, en, rows: [{k, v}], note }``。
 *
 * ★ 2026-10-16 改的小动效（按要求）：播放前每张卡**只露标题**
 *   （「01 隐藏阶群 GROUP」），数据流扫过它之后卡片**长开**、露出里面的
 *   参数键值对。实现用 `grid-template-rows: 0fr → 1fr`，高度是真动画，
 *   不是 `v-show` 那种硬切的显示/隐藏。
 *
 * ★ 2026-10-19（用户：太长）：播完之后**整条流水线收起成一行**——
 *   只留「01 隐藏阶群 ✓ › 02 位置素数 ✓ › …」，参数明细与注解全部不再铺开；
 *   要看具体参数值走下方「系统参数」那一组卡片。播放过程中仍逐张展开。
 */
import { ref, computed } from 'vue'

const props = defineProps({
  /** 六张卡（顺序即流转顺序）。 */
  steps: { type: Array, default: () => [] },
  /** 每张卡点亮后停留多久再点亮下一张（毫秒）。 */
  stepMs: { type: Number, default: 620 },
})

const emit = defineEmits(['done'])

/** 已经点亮了几张（0 = 还没开始）。 */
const lit = ref(0)
const playing = ref(false)
const done = ref(false)

const litCount = computed(() => lit.value)

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

/** 从第一张开始依次点亮；播放中再点不会叠加（幂等）。 */
async function play() {
  if (playing.value) return
  playing.value = true
  done.value = false
  lit.value = 0
  await sleep(160)
  for (let i = 0; i < props.steps.length; i++) {
    lit.value = i + 1
    await sleep(props.stepMs)
  }
  playing.value = false
  done.value = true
  emit('done')
}

/** 回到「未开始」的初始态（卡片收起、只剩标题）。 */
function reset() {
  lit.value = 0
  playing.value = false
  done.value = false
}

/** 直接呈现**完成态**（不再播动画）—— 同一次登录里再进这一页时用。 */
function finish() {
  lit.value = props.steps.length
  playing.value = false
  done.value = true
}

defineExpose({ play, reset, finish })
</script>

<template>
  <div class="init-flow" :class="{ 'is-playing': playing, 'is-done': done }" aria-label="系统参数初始化流水线">
    <div class="if-meta mono">
      <span>INIT PIPELINE</span>
      <span class="if-meta-sep">·</span>
      <span>{{ litCount }} / {{ steps.length }} PARAMS RESOLVED</span>
      <span v-if="playing" class="if-meta-live">FLOWING</span>
      <span v-else-if="done" class="if-meta-done">READY</span>
    </div>

    <!-- 播完之后：整条流水线收起成一行（只留序号 + 步骤名 + 勾） -->
    <div v-if="done && !playing" class="if-brief">
      <template v-for="(s, i) in steps" :key="s.key">
        <span class="if-brief-item">
          <span class="mono if-brief-no">{{ String(s.no).padStart(2, '0') }}</span>
          <span class="if-brief-title">{{ s.title }}</span>
          <span class="if-brief-tick">✓</span>
        </span>
        <span v-if="i < steps.length - 1" class="if-brief-sep" aria-hidden="true">›</span>
      </template>
    </div>

    <div v-else class="if-stack">
    <div v-for="(s, i) in steps" :key="s.key" class="if-slot">
      <div
        class="if-card"
        :class="{ 'is-idle': lit <= i, 'is-live': playing && lit === i + 1, 'is-on': lit > i }"
      >
        <div class="if-head">
          <span class="if-no mono">{{ String(s.no).padStart(2, '0') }}</span>
          <span class="if-title">{{ s.title }}</span>
          <span class="if-en mono">{{ s.en }}</span>
          <span class="if-state mono">
            <span v-if="lit > i" class="if-tick">✓ 已定</span>
            <span v-else-if="playing && lit === i + 1" class="if-now">写入中…</span>
            <span v-else>待初始化</span>
          </span>
        </div>

        <!-- ★ 参数区：播放前高度为 0（只露标题），数据流扫过后长开 -->
        <div class="if-body" :class="{ 'is-open': lit > i }">
          <div class="if-body-inner">
            <ul class="if-rows">
              <li v-for="(r, j) in s.rows" :key="j" class="if-row">
                <span class="if-k">{{ r.k }}</span>
                <span class="if-v mono">{{ r.v }}</span>
              </li>
            </ul>
            <p v-if="s.note" class="if-note">{{ s.note }}</p>
          </div>
        </div>

        <!-- 点亮时的扫描光带（只在当前这一张身上跑） -->
        <span v-if="playing && lit === i + 1" class="if-scan" aria-hidden="true" />
      </div>

      <!-- 连接段：数据包沿它往下流；下一张点亮后这一段变成「已通过」 -->
      <div
        v-if="i < steps.length - 1"
        class="if-link"
        :class="{ 'is-on': lit > i + 1, 'is-flow': playing && lit >= i + 1 }"
      >
        <span class="if-line" />
        <span v-if="playing && lit >= i + 1 && lit <= i + 2" class="if-pack" />
      </div>
    </div>
    </div>
  </div>
</template>

<style scoped>
/* ★ 单色 + 等宽：一眼能看出与总览那排彩色原理卡不是一套东西。 */
.init-flow {
  position: relative;
  border: 1px solid var(--line);
  border-radius: 12px;
  padding: 14px 16px 6px;
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--accent) 5%, transparent), transparent 90px),
    var(--bg-raised);
}
.if-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  font-size: 12px;
  letter-spacing: .14em;
  color: var(--text-3);
}
.if-meta-sep { color: var(--text-3); }
.if-meta-live { color: var(--accent); animation: if-pulse 1s ease-in-out infinite; }
.if-meta-done { color: var(--ok); }

.if-slot { position: relative; }

/* 播完后的一行摘要：只有序号、步骤名与勾 —— 参数明细交给下方「系统参数」。 */
.if-brief { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 8px; padding: 2px 0 10px; animation: if-brief-in .26s ease; }
.if-brief-item {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  padding: 3px 9px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--bg-panel);
}
.if-brief-no { font-size: 11px; color: var(--accent); letter-spacing: .1em; }
.if-brief-title { font-size: 13px; color: var(--text-1); }
.if-brief-tick { font-size: 11px; color: var(--ok); }
.if-brief-sep { color: var(--text-3); font-size: 12px; }

.if-card {
  position: relative;
  overflow: hidden;
  border: 1px solid var(--line);
  border-left: 2px solid var(--line);
  border-radius: 10px;
  padding: 10px 12px;
  background: var(--bg-panel);
  transition: border-color .3s ease, background .3s ease, opacity .3s ease, transform .3s ease;
}
/* 未选定：整体压暗，但**看得见它在等什么** */
.if-card.is-idle { opacity: .5; }
.if-card.is-live {
  border-color: color-mix(in srgb, var(--accent) 45%, transparent);
  border-left-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 7%, var(--bg-panel));
  transform: translateX(2px);
}
.if-card.is-on {
  border-left-color: color-mix(in srgb, var(--accent) 70%, transparent);
}

.if-head {
  display: flex;
  align-items: baseline;
  gap: 9px;
}
.if-no {
  font-size: 12px;
  color: var(--accent);
  letter-spacing: .1em;
}
.if-title { color: var(--text-1); font-size: 14px; }
.if-en { color: var(--text-3); font-size: 11px; letter-spacing: .12em; }
.if-state { margin-left: auto; font-size: 12px; color: var(--text-3); }
.if-tick { color: var(--ok); }
.if-now { color: var(--accent); }

.if-rows { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
.if-row { display: flex; align-items: baseline; gap: 14px; font-size: 13px; }
.if-k { color: var(--text-2); flex: none; min-width: 132px; }
.if-v { color: var(--text-1); word-break: break-all; }
.if-note { margin: 7px 0 0; color: var(--text-3); font-size: 12px; line-height: 1.6; }

/* ★ 参数区的高度动画：0fr → 1fr（真正的“长开”，不是硬切显示）。 */
.if-body {
  display: grid;
  grid-template-rows: 0fr;
  opacity: 0;
  transition: grid-template-rows .38s cubic-bezier(.2, .7, .3, 1), opacity .28s ease;
}
.if-body.is-open {
  grid-template-rows: 1fr;
  opacity: 1;
}
.if-body-inner {
  min-height: 0;
  overflow: hidden;
}
.if-body.is-open .if-body-inner { padding-top: 8px; }

.if-scan {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  width: 40%;
  background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--accent) 18%, transparent), transparent);
  animation: if-scan 0.9s linear infinite;
  pointer-events: none;
}

/* 纵向连线 + 数据包 */
.if-link {
  position: relative;
  height: 26px;
  margin-left: 15px;
  width: 2px;
}
.if-line {
  position: absolute;
  inset: 0 auto 0 0;
  width: 2px;
  background: var(--line);
}
.if-link.is-on .if-line { background: color-mix(in srgb, var(--accent) 55%, transparent); }
.if-link.is-flow .if-line { background: linear-gradient(180deg, var(--accent), color-mix(in srgb, var(--accent) 20%, transparent)); }
.if-pack {
  position: absolute;
  left: -3px;
  top: 0;
  width: 8px;
  height: 8px;
  border-radius: 2px;
  background: var(--accent);
  box-shadow: 0 0 8px 2px color-mix(in srgb, var(--accent) 45%, transparent);
  animation: if-drop 0.62s ease-in forwards;
}

@keyframes if-scan { 0% { transform: translateX(-110%); } 100% { transform: translateX(340%); } }
@keyframes if-drop { 0% { transform: translateY(-2px); opacity: 0; } 30% { opacity: 1; } 100% { transform: translateY(24px); opacity: 0; } }
@keyframes if-pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }
@keyframes if-brief-in { from { opacity: 0; transform: translateY(-3px); } to { opacity: 1; transform: none; } }

@media (max-width: 560px) {
  .if-k { min-width: 96px; }
}
</style>
