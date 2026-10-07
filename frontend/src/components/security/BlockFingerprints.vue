<script setup>
/**
 * 一份证据「逐块」的分量指纹列表 —— 证据池卡片与完整性验证页共用。
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
  //: 最多列多少行（1024 块的证据铺出来太长，超了就截断并说明）。
  limit: { type: Number, default: 256 },
  //: 指纹显示多少位十六进制。
  len: { type: Number, default: 24 },
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
  if (r && r.block_idx !== undefined && r.block_idx !== null) {
    return `第 ${r.block_idx} 块`
  }
  return String(gi)
}

/** 部分结果时，缺的那几块要说出来 —— 不然"少了几个下标"要靠人自己数。 */
function missing(gi, i) {
  return !props.values?.[i]
}
</script>

<template>
  <div class="fp-list">
    <div v-for="(gi, i) in shown" :key="gi" class="fp-row">
      <span class="mono idx">{{ labelOf(gi) }}</span>
      <HashText v-if="!missing(gi, i)" :value="values[i]" :len="len" />
      <span v-else class="text-3" style="font-size: 12px">（这次没取到）</span>
      <span v-if="srcOf(gi)" class="src text-3">{{ srcOf(gi) }}</span>
    </div>
    <p v-if="truncated" class="note" style="margin: 6px 0 0">
      只列了前 {{ limit }} 块（这一份共 {{ indices.length }} 块）—— 指纹的位数与含义不变。
    </p>
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
.idx {
  font-size: 12px;
  color: var(--text-2);
}
.src {
  font-size: 11px;
}
</style>
