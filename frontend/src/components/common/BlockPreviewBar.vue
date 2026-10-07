<script setup>
/**
 * 「超过 N 块就收起来」的那条横条 —— 块分布矩阵与块明细表共用。
 *
 * ★ 为什么抽成一个组件：这两处讲的是同一件事（哪一块在谁手上），
 *   拆成两份迟早会一处写「显示全部」、另一处写「展示详情」——
 *   同一个动作出现两个名字，用户得猜它们是不是一回事。
 *   文案与阈值只在这里/常量里各写一份。
 *
 * ★ 只有真的被收起来时才渲染（`total > limit`）：小文件不该多一条
 *   "共 12 块 · 已全部列出" 的废话，更不该多一次点击。
 */
import { computed } from 'vue'

const props = defineProps({
  /** 一共多少块。 */
  total: { type: Number, default: 0 },
  /** 收起来时先列多少块（<=0 表示不限，那就永远不渲染这条）。 */
  limit: { type: Number, default: 0 },
  /** 当前是不是已展开。 */
  expanded: { type: Boolean, default: false },
})

defineEmits(['toggle'])

const collapsed = computed(() => props.limit > 0 && props.total > props.limit)

/** 收起来了多少块（展开后为 0）。 */
const hidden = computed(() => Math.max(0, props.total - props.limit))
</script>

<template>
  <div v-if="collapsed" class="preview-bar">
    <span v-if="expanded">共 {{ total }} 块 · 已全部展开</span>
    <span v-else>
      共 {{ total }} 块 · 先列前 {{ limit }} 块，还有 <b>{{ hidden }}</b> 块收起来了
    </span>
    <el-button link type="primary" size="small" @click="$emit('toggle')">
      {{ expanded ? '收起详情' : '展示详情' }}
    </el-button>
  </div>
</template>

<style scoped>
.preview-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 2px 0 8px;
  font-size: 12px;
  color: var(--text-3);
}
.preview-bar b {
  color: var(--text-2);
}
</style>
