<script setup>
/**
 * 「超过 N 块即收起」的横条。分布矩阵与分量明细表共用。
 *
 * 抽为独立组件的原因：两处展示同一事实（哪一块由谁持有）。
 * 若各写一份，同一动作会出现不同措辞与阈值。
 * 文案与阈值只在本组件与常量中各定义一次。
 *
 * 仅在确实收起时渲染（`total > limit`）：未收起时不显示多余信息，
 * 也不产生额外点击。
 */
import { computed } from 'vue'

const props = defineProps({
  /** 一共多少块。 */
  total: { type: Number, default: 0 },
  /** 收起时先列出的块数；<= 0 表示不限，此时不渲染本组件。 */
  limit: { type: Number, default: 0 },
  /** 当前是否已展开。 */
  expanded: { type: Boolean, default: false },
})

defineEmits(['toggle'])

const collapsed = computed(() => props.limit > 0 && props.total > props.limit)

/** 被收起的块数；展开后为 0。 */
const hidden = computed(() => Math.max(0, props.total - props.limit))
</script>

<template>
  <div v-if="collapsed" class="preview-bar">
    <span v-if="expanded">共 {{ total }} 块 · 已全部展开</span>
    <span v-else>
      共 {{ total }} 块 · 先列前 {{ limit }} 块，其余 <b>{{ hidden }}</b> 块已收起
    </span>
    <el-button link type="primary" size="small" @click="$emit('toggle')">
      {{ expanded ? '收起详情' : '展开详情' }}
    </el-button>
  </div>
</template>

<style scoped>
.preview-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  /* ★ 允许换行：这一条现在也会出现在**四分之一宽**的分布矩阵面板里，
     不让它换行就会把「展开详情」按钮挤出面板。 */
  flex-wrap: wrap;
  margin: 2px 0 8px;
  font-size: 13px;
  color: var(--text-3);
}
.preview-bar b {
  color: var(--text-2);
}
</style>
