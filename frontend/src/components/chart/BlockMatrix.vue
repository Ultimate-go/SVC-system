<script setup>
/**
 * 块分布矩阵：行 = 存储槽位（块），列 = 节点。
 * 格子颜色区分「主副本 / 副本 / 不在我这」。
 *
 * 数据源：
 *  - nodes: /api/nodes 的数组（每行有 indices[]）
 *  - layout: 可选，/api/files/{id} 的 layout[].replicas（主副本在前）
 *
 * 若传 layout，用 replicas 判主/副本（更精确）；否则退化为「在/不在」。
 *
 * ★ 格子的悬浮提示里会附上这一块的**分量指纹**（需要 layout 里带 element
 *   —— 只有文件详情页的"详细"模式才会要那份数据）。对着矩阵一块块看时，
 *   "这一格到底是什么" 比 "块号@节点" 有用得多。
 */
import { computed, ref } from 'vue'
import { hexFp } from '../../utils/format'
import { BLOCK_PREVIEW_LIMIT } from '../../utils/constants.js'
import BlockPreviewBar from '../common/BlockPreviewBar.vue'

const props = defineProps({
  nodes: { type: Array, default: () => [] },
  layout: { type: Array, default: null },
  /**
   * 先渲染多少块（超过就收起来，点「展示详情」再看剩下的）。
   *
   * ★ 默认 16（`BLOCK_PREVIEW_LIMIT`）：几十上百块一路铺出来会把页面拉得很长，
   *   而真正想看的“这几块在谁手上”反而被淹掉（用户反馈）。
   *   ≤ 16 块时不收、也不显示那条横条 —— 小文件不该多一次点击。
   *
   * ★ 不默认“全部”还有渲染成本的原因：块数现在没有上限，几千行一次性
   *   铺出来会卡（与 `n_max` 无关，纯粹是渲染成本）。
   */
  maxBlocks: { type: Number, default: BLOCK_PREVIEW_LIMIT },
})

/** 用户点了“显示全部”没有。 */
const expanded = ref(false)

const nodeIds = computed(() => props.nodes.map((n) => n.node_id))

/**
 * 有没有 layout（每块的 replicas）决定了我们**知道多少**：
 * 有 layout 才分得出主副本 / 副本，没 layout 只知道在不在。
 * 图例得跟着变，否则会凭空承诺一个图上永远不会出现的颜色。
 */
const hasLayout = computed(() => Array.isArray(props.layout) && props.layout.length > 0)

// 每个全局下标 → { holder, replicas, element }
const cellInfo = computed(() => {
  const map = {}
  if (props.layout) {
    for (const b of props.layout) {
      const reps = Array.isArray(b.replicas) ? b.replicas : [b.holder]
      map[b.global_index] = {
        holder: b.holder || reps[0],
        replicas: reps,
        //: 公开分量（十进制）。只有"详细"模式后端才会给 —— 不给就是 undefined。
        element: b.element,
      }
    }
  }
  return map
})

/**
 * 格子的悬浮提示。
 *
 * ★ 必须先说清这格是什么状态：早先的写法不管状态一律写「块 N @ 节点」，
 *   于是「不在我这」的格子也在宣称自己存着这一块 —— 和格子颜色自相矛盾。
 */
function cellTip(gi, nid) {
  const label = rowLabel(gi)
  const state = cellState(gi, nid)
  if (state === 'none') return `${label} 不在 ${nid}`
  const head = `${label} @ ${nid}（${state === 'primary' ? '主副本' : '副本'}）`
  const el = cellInfo.value[gi]?.element
  return el ? `${head} · 分量指纹 ${hexFp(el, 16)}` : head
}

// 全局下标列表（优先 layout，否则各节点 indices 并集）
const indices = computed(() => {
  if (props.layout) {
    return props.layout.map((b) => b.global_index).sort((a, b) => a - b)
  }
  const set = new Set()
  for (const n of props.nodes) for (const i of n.indices || []) set.add(i)
  return [...set].sort((a, b) => a - b)
})

const shownIndices = computed(() => {
  const limit =
    props.maxBlocks > 0 && !expanded.value ? props.maxBlocks : indices.value.length
  return indices.value.slice(0, limit)
})

/**
 * 行头怎么说：**第几块**，而不是把全局下标摆到图上。
 *
 * ★★ 全局下标是**内部坐标**（密文在节点上的槽位号，由上传顺序决定），
 *   它既不是“第几块”（那个由块身份派生）、也不该让人记。
 *   行头摆它等于把内部坐标摆在图上（用户反馈）。
 *
 *   块号来自 ``layout``（文件详情页会给，每行带 ``block_idx``）；
 *   只有“没传 layout、只有各节点 indices”那种退化用法才算不出块号 ——
 *   那时如实退回数字，不编一个假的块号。
 */
const blockNoOf = computed(() => {
  const m = new Map()
  for (const b of props.layout || []) {
    if (b && b.global_index !== undefined && b.block_idx !== undefined && b.block_idx !== null) {
      m.set(Number(b.global_index), Number(b.block_idx))
    }
  }
  return m
})

function rowLabel(gi) {
  const b = blockNoOf.value.get(Number(gi))
  return b === undefined ? String(gi) : `第 ${b} 块`
}

function cellState(gi, nid) {
  const info = cellInfo.value[gi]
  if (info) {
    if (info.replicas && info.replicas.length > 1) {
      if (info.holder === nid) return 'primary'
      if (info.replicas.includes(nid)) return 'replica'
      return 'none'
    }
    // 单副本：holder 即持有者
    if (info.holder === nid || (info.replicas && info.replicas[0] === nid)) return 'primary'
    return 'none'
  }
  // 无 layout：按节点 indices 判在/不在
  const node = props.nodes.find((n) => n.node_id === nid)
  if (node && (node.indices || []).includes(gi)) return 'primary'
  return 'none'
}
</script>

<template>
  <div class="block-matrix-wrap">
    <BlockPreviewBar
      :total="indices.length"
      :limit="maxBlocks"
      :expanded="expanded"
      @toggle="expanded = !expanded"
    />
    <div class="matrix-scroll">
      <table class="matrix">
        <thead>
          <tr>
            <th class="corner">块 \ 节点</th>
            <th v-for="nid in nodeIds" :key="nid" class="col-head mono">{{ nid }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="gi in shownIndices" :key="gi">
            <td class="row-head mono">{{ rowLabel(gi) }}</td>
            <td v-for="nid in nodeIds" :key="nid" class="cell">
              <span class="dot" :class="cellState(gi, nid)" :title="cellTip(gi, nid)" />
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="legend">
      <span class="lg"><span class="dot primary" />{{ hasLayout ? '主副本' : '持有' }}</span>
      <span v-if="hasLayout" class="lg"><span class="dot replica" />副本</span>
      <span class="lg"><span class="dot none" />不在</span>
    </div>
  </div>
</template>
<style scoped>
.block-matrix-wrap {
  overflow: hidden;
}
/* “还有多少块收起来了”那条横条在 `common/BlockPreviewBar.vue` 里（两处共用）。 */
.matrix-scroll {
  /* ★ 展开后最高这么高，超出**在矩阵内部滚** —— 否则一份上千块的文件
     点一下「展示详情」就把整个页面拉成几千行（那等于把页面毁了）。
     ≤16 块时这段高度永远用不到，小文件跟以前一模一样。 */
  max-height: 460px;
  overflow: auto;
}
/* 行多、内部滚动时表头跟着走 —— 滚到第 200 行还知道自己在看哪一列。 */
.matrix thead th {
  position: sticky;
  top: 0;
  z-index: 1;
}
.matrix {
  border-collapse: collapse;
  font-size: 12px;
}
.matrix th,
.matrix td {
  padding: 4px 6px;
  text-align: center;
  border: 1px solid var(--line);
}
.col-head,
.row-head,
.corner {
  background: var(--bg-raised);
  color: var(--text-2);
  font-weight: 400;
  white-space: nowrap;
}
.corner {
  text-align: left;
}
.dot {
  display: inline-block;
  width: 12px;
  height: 12px;
  border-radius: 3px;
}
.dot.primary {
  background: var(--accent);
}
.dot.replica {
  background: var(--accent-2);
}
.dot.none {
  background: var(--bg-raised);
  border: 1px solid var(--line);
}
.legend {
  display: flex;
  gap: 16px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-2);
}
.lg {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.truncate-note {
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-3);
}
</style>
