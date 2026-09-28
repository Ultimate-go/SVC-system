<script setup>
/**
 * 块分布矩阵：行 = 全局下标（块），列 = 节点。
 * 格子颜色区分「主副本 / 副本 / 不在我这」。
 *
 * 数据源：
 *  - nodes: /api/nodes 的数组（每行有 indices[]）
 *  - layout: 可选，/api/files/{id} 的 layout[].replicas（主副本在前）
 *
 * 若传 layout，用 replicas 判主/副本（更精确）；否则退化为「在/不在」。
 */
import { computed } from 'vue'

const props = defineProps({
  nodes: { type: Array, default: () => [] },
  layout: { type: Array, default: null },
  maxCols: { type: Number, default: 30 },
})

const nodeIds = computed(() => props.nodes.map((n) => n.node_id))

// 每个全局下标 → { holder, replicas }
const cellInfo = computed(() => {
  const map = {}
  if (props.layout) {
    for (const b of props.layout) {
      const reps = Array.isArray(b.replicas) ? b.replicas : [b.holder]
      map[b.global_index] = { holder: b.holder || reps[0], replicas: reps }
    }
  }
  return map
})

// 全局下标列表（优先 layout，否则各节点 indices 并集）
const indices = computed(() => {
  if (props.layout) {
    return props.layout.map((b) => b.global_index).sort((a, b) => a - b)
  }
  const set = new Set()
  for (const n of props.nodes) for (const i of n.indices || []) set.add(i)
  return [...set].sort((a, b) => a - b)
})

const shownIndices = computed(() => indices.value.slice(0, props.maxCols))

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
            <td class="row-head mono">{{ gi }}</td>
            <td v-for="nid in nodeIds" :key="nid" class="cell">
              <span class="dot" :class="cellState(gi, nid)" :title="`${gi} @ ${nid}`" />
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <div class="legend">
      <span class="lg"><span class="dot primary" />主副本</span>
      <span class="lg"><span class="dot replica" />副本</span>
      <span class="lg"><span class="dot none" />不在</span>
    </div>
    <div v-if="indices.length > maxCols" class="truncate-note">
      仅显示前 {{ maxCols }} 块（共 {{ indices.length }} 块）
    </div>
  </div>
</template>

<style scoped>
.block-matrix-wrap {
  overflow: hidden;
}
.matrix-scroll {
  overflow-x: auto;
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
