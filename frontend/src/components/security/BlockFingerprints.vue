<script setup>
/**
 * 一份证据「逐块」的分量指纹列表 —— 聚合面板的卡片与可验证查询页共用。
 *
 * 为什么单独抽出来：这两处的数据形状**完全一样**（`indices[i]` ↔ `values[i]`，
 * 都是 `/api/query` 或分解/聚合出来的同一套东西），而"下标 + 指纹 + 来源"
 * 这三列的排版也一模一样。写两份迟早会分叉（比如一边改了悬浮提示的位数）。
 *
 * ★ 它只负责"列出来"，**不负责决定要不要显示** —— 那个由调用方 `v-if`
 *   （跟着「简略 / 详细」开关，或者卡片自己的展开状态）。这样同一个组件
 *   既能被全局开关控制，也能被单张卡片的「细看」按钮控制。
 */
import { computed } from 'vue'
import HashText from '../common/HashText.vue'

const props = defineProps({
  //: 全局下标（与 values 一一对应、同序）。
  indices: { type: Array, default: () => [] },
  //: 每个下标对应的分量（十进制字符串），来自 /api/query 的 values。
  values: { type: Array, default: () => [] },
  //: 可选：下标 → 它属于哪个文件的哪一块（/api/query 的 refs）。
  refs: { type: Array, default: () => [] },
  //: 最多列多少行（1024 块的证据铺出来太长，超了就删除并说明）。
  limit: { type: Number, default: 256 },
  //: 指纹显示多少位十六进制。
  len: { type: Number, default: 24 },
  /**
   * 是否在每行右侧再列一遍“这份块属于谁”。
   *
   * ★ 默认 true（聚合面板的卡片只有这一处能看出归属）。可验证查询页把
   *   指纹与「逐块归属」合进同一个折叠后，下面已经有完整的
   *   ``第 N 块 → owner/file_key + holder``，这里再列一遍就是纯重复 ——
   *   那边传 ``false``。
   */
  showSource: { type: Boolean, default: true },
})

const shown = computed(() => props.indices.slice(0, props.limit))
const truncated = computed(() => props.indices.length > shown.value.length)

const refMap = computed(() => {
  const m = {}
  for (const r of props.refs || []) m[r.global_index] = r
  return m
})

function srcOf(gi) {
  const r = refMap.value[gi]
  if (!r) return ''
  if (r.owner) return `${r.owner} / ${r.file_key}`
  return ''
}

/**
 * 这一行**左边的标签**。
 *
 * ★★ 说「第几块」而不是把全局下标摆出来 —— 后者是**内部坐标**（由上传顺序
 *   决定），界面上只说「哪份文件的第几块」。块号算不出来（很旧的卡没存
 *   ``refs``）才如实退回数字，**不假装它是个块号**。
 */
function labelOf(gi) {
  const r = refMap.value[gi]
  // ★ 界面上按 **1 起** 编号（见 utils/format.js 的坐标口径）
  if (r && r.block_idx !== undefined && r.block_idx !== null) {
    return `第 ${r.block_idx + 1} 块`
  }
  return String(Number(gi) + 1)
}

/** 部分结果时，缺的那几块要说出来 —— 不然"少了几个下标"要靠人自己数。 */
function missing(gi, i) {
  return !props.values?.[i]
}
</script>

<template>
  <div class="fp-list" :class="{ 'no-src': !showSource }">
    <div v-for="(gi, i) in shown" :key="gi" class="fp-row">
      <span class="mono idx">{{ labelOf(gi) }}</span>
      <HashText v-if="!missing(gi, i)" :value="values[i]" :len="len" />
      <span v-else class="text-3" style="font-size: 13px">（这次没取到）</span>
      <span v-if="showSource && srcOf(gi)" class="src text-3">{{ srcOf(gi) }}</span>
    </div>
    <details v-if="truncated" class="note-collapse">
      <summary>说明</summary>
      <p class="note" style="margin: 6px 0 0">
        只列了前 {{ limit }} 块（这一份共 {{ indices.length }} 块）—— 指纹的位数与含义不变。
      </p>
    </details>
  </div>
</template>

<style scoped>
.fp-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  margin-top: 6px;
  max-height: 260px;
  overflow-y: auto;
}
.fp-row {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr) minmax(0, 1fr);
  align-items: baseline;
  gap: 10px;
  padding: 2px 0;
  border-bottom: 1px dashed var(--line);
}
.fp-row:last-child {
  border-bottom: none;
}
/* 不列来源列时只留两列：指纹那一列吃满剩下的宽度（否则右边空一半）。 */
.fp-list.no-src .fp-row {
  grid-template-columns: 64px minmax(0, 1fr);
}
.idx {
  font-size: 13px;
  color: var(--text-2);
}
.src {
  font-size: 13px;
}
</style>
