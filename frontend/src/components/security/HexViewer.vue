<script setup>
/**
 * 分块十六进制视图（010 Editor 风格）：左书签栏 + 每块一张卡（偏移 / 16 字节 / ASCII）。
 *
 * ★ 详情页与上传文件页共用这一份 —— 两处的“解密结果”应当长得一模一样：
 *   左边竖排的分块书签、每块一种色相、块头写明长度与全文偏移。
 *   数据由 `utils/hexview.js::buildHexBlocks` 统一构造，组件只负责渲染与滚动同步。
 * ★ 只读、不持有业务状态；块多时靠书签跳转（滚动也会反向点亮当前块）。
 */
import { nextTick, ref, watch } from 'vue'

const props = defineProps({
  /** `buildHexBlocks` 的产物。 */
  blocks: { type: Array, default: () => [] },
})

const hexWrap = ref(null)
/** 当前点亮的分块序号（0 起）；-1 = 还没定。 */
const activeBlock = ref(-1)
/** 分块序号 → 该块的 DOM（`v-for` 里的函数 ref 登记）。 */
const blockEls = new Map()

function setBlockEl(el, idx) {
  if (el) blockEls.set(idx, el)
  else blockEls.delete(idx)
}

/** 点书签：只滚容器，不连带滚页面 / 弹框。 */
function jumpToBlock(idx) {
  const wrap = hexWrap.value
  const el = blockEls.get(idx)
  if (!wrap || !el) return
  const offset = el.getBoundingClientRect().top - wrap.getBoundingClientRect().top
  wrap.scrollTo({ top: wrap.scrollTop + offset - 6, behavior: 'smooth' })
  activeBlock.value = idx
}

/**
 * 滚到哪儿就把哪一枚点亮：取「顶边已经越过容器顶」的**序号最大**的那一块。
 *
 * ★ 按块的边界算而不是按滚动比例算 —— 块高不一样（最后一块往往不满），
 *   按比例算会和实际看到的那一块对不上。
 * ★ 不能依赖 `blockEls` 的迭代顺序（Map 记的是插入顺序，重渲染后不保证升序），
 *   所以取最大值而不是“最后一个命中的”。
 * ★ 滚动事件很密（128 块时一次要量 128 个盒子），用 rAF 合并到一帧一次。
 */
let scrollRaf = 0
function syncActiveBlock() {
  if (scrollRaf) return
  scrollRaf = requestAnimationFrame(() => {
    scrollRaf = 0
    const wrap = hexWrap.value
    if (!wrap) return
    const line = wrap.getBoundingClientRect().top + 8
    let cur = -1
    for (const [idx, el] of blockEls) {
      if (idx > cur && el.getBoundingClientRect().top <= line) cur = idx
    }
    if (cur >= 0) activeBlock.value = cur
  })
}

// 每次拿到新的一批块（弹框是 destroy-on-close，内容会重建）都从第 1 块看起。
watch(
  () => props.blocks.length,
  async (len) => {
    if (!len) return
    await nextTick()
    if (hexWrap.value) hexWrap.value.scrollTop = 0
    activeBlock.value = props.blocks[0]?.idx ?? -1
  },
  { immediate: true },
)
</script>

<template>
  <div class="hx-main">
    <!--
      ★ 书签栏：一个分块一枚，颜色与该块的左色条 / 标题同色，点一下就滚到那一块。
        块多的时候（128 块 ≈ 8000+ 行）靠滚轮拖很久，这里给一条索引；
        它自己也能滚，且滚到哪儿就亮哪一枚。
    -->
    <div v-if="blocks.length > 1" class="hx-rail" aria-label="分块书签">
      <button
        v-for="b in blocks"
        :key="b.idx"
        type="button"
        class="hx-tab"
        :class="{ 'is-now': b.idx === activeBlock }"
        :style="{ '--hx-hue': String(b.hue) }"
        :title="`第 ${b.idx + 1} 块 · ${b.len} 字节 · 偏移 0x${b.offsetHex}`"
        @click="jumpToBlock(b.idx)"
      >
        {{ b.idx + 1 }}
      </button>
    </div>
    <div ref="hexWrap" class="hx-wrap" @scroll.passive="syncActiveBlock">
      <div
        v-for="b in blocks"
        :key="b.idx"
        :ref="(el) => setBlockEl(el, b.idx)"
        class="hx-block"
        :style="{ borderLeftColor: `hsl(${b.hue} 62% 55%)` }"
      >
        <div class="hx-head" :style="{ color: `hsl(${b.hue} 58% 30%)` }">
          第 {{ b.idx + 1 }} 块
          <span class="hx-sep">·</span>
          {{ b.len }} 字节
          <span class="hx-sep">·</span>
          <span class="mono">偏移 0x{{ b.offsetHex }}</span>
          <span v-if="b.replayed" class="hx-replay">服务端返回的是旧版本</span>
        </div>
        <div class="hx-body">
          <div v-for="r in b.rows" :key="r.off" class="hx-row">
            <span class="hx-off">{{ r.offHex }}</span>
            <span class="hx-bytes">
              <span
                v-for="(c, i) in r.cells"
                :key="i"
                class="hx-c"
                :class="{ 'hx-zero': c.zero }"
              >{{ c.hex }}</span>
            </span>
            <span class="hx-ascii">
              <span v-for="(ch, i) in r.ascii" :key="i" :class="{ 'hx-dotc': ch.dot }">{{ ch.ch }}</span>
            </span>
          </div>
        </div>
      </div>
      <p v-if="!blocks.length" class="text-2" style="font-size: 13px">没有可展示的分块。</p>
    </div>
  </div>
</template>

<style scoped>
/* 左书签栏 + 右十六进制视图。书签颜色由 `--hx-hue` 控制，与块的左色条同源。 */
.hx-main {
  display: flex;
  gap: 10px;
  margin-top: 10px;
  align-items: stretch;
}
.hx-rail {
  flex: 0 0 auto;
  width: 54px;
  max-height: 58vh;
  overflow: auto;
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--bg-raised);
  scrollbar-width: thin;
}
.hx-tab {
  flex: 0 0 auto;
  height: 30px;
  padding: 0;
  border: 0;
  border-left: 3px solid hsl(var(--hx-hue) 62% 45%);
  border-radius: 4px;
  background: hsl(var(--hx-hue) 55% 92%);
  color: hsl(var(--hx-hue) 58% 32%);
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.12s, color 0.12s, transform 0.12s;
}
.hx-tab:hover {
  background: hsl(var(--hx-hue) 62% 86%);
  color: hsl(var(--hx-hue) 65% 24%);
  transform: translateX(2px);
}
.hx-tab:focus-visible {
  outline: 2px solid hsl(var(--hx-hue) 70% 50%);
  outline-offset: 1px;
}
.hx-tab.is-now {
  background: hsl(var(--hx-hue) 62% 88%);
  color: hsl(var(--hx-hue) 68% 24%);
  font-weight: 600;
  box-shadow: inset 0 0 0 1px hsl(var(--hx-hue) 55% 60%);
}

.hx-wrap {
  flex: 1 1 auto;
  min-width: 0;
  max-height: 58vh;
  overflow: auto;
  padding: 10px 8px 4px 0;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--bg-raised);
}
.hx-block {
  margin: 0 0 10px 8px;
  border-left: 3px solid #4a7fb5;
  border-radius: 6px;
  background: var(--bg-panel);
  overflow: hidden;
}
.hx-block:nth-child(even) { background: color-mix(in srgb, var(--bg-panel) 88%, var(--bg-page)); }
.hx-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding: 5px 10px;
  font-size: 12.5px;
  font-weight: 600;
  background: color-mix(in srgb, var(--text-1) 4%, transparent);
  border-bottom: 1px solid var(--line);
}
.hx-sep { opacity: 0.35; }
.hx-replay { margin-left: auto; font-size: 12px; font-weight: 500; color: #b45309; }
.hx-body { padding: 6px 10px 8px; }
.hx-row {
  display: grid;
  grid-template-columns: 82px minmax(0, 1fr) 176px;
  gap: 10px;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.75;
}
.hx-row:hover { background: color-mix(in srgb, var(--accent) 8%, transparent); }
.hx-off { color: var(--text-3); }
.hx-bytes { display: flex; flex-wrap: wrap; }
.hx-c { width: 1.95em; text-align: center; color: var(--text-1); }
.hx-zero { color: var(--text-3); opacity: 0.6; }
.hx-ascii {
  color: var(--text-2);
  padding-left: 10px;
  border-left: 1px solid var(--line);
  letter-spacing: 0.5px;
  word-break: break-all;
}
.hx-dotc { color: var(--text-3); opacity: 0.6; }
@media (max-width: 900px) {
  .hx-row { grid-template-columns: 74px minmax(0, 1fr); }
  .hx-ascii { display: none; }
}
</style>
