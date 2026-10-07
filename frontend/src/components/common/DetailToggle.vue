<script setup>
/**
 * 「简略 / 详细」开关 —— 控制要不要把**每一块的指纹**铺出来。
 *
 * 它不是一个页面的局部状态，而是**界面偏好**：跟着主题/密度那一套走
 * （`stores/theme.js` → localStorage）。在一页里切到"详细"，翻到另一页
 * 也该还是详细 —— 否则每换一个页面都要重新点一遍。
 *
 * 默认给「详细」：这一版就是想把密码学细节摆出来看。
 */
import { computed } from 'vue'
import { useThemeStore } from '../../stores/theme'

const theme = useThemeStore()
const mode = computed(() => theme.detailMode)
</script>

<template>
  <div class="detail-toggle">
    <span class="tip text-3">{{ mode === 'detail' ? '正在显示每块的指纹（悬浮看完整值）' : '已折叠指纹，只显示块号、长度与所在节点' }}</span>
    <el-radio-group :model-value="mode" size="small" @change="(v) => theme.setDetail(v)">
      <el-radio-button value="brief">简略</el-radio-button>
      <el-radio-button value="detail">详细</el-radio-button>
    </el-radio-group>
  </div>
</template>

<style scoped>
.detail-toggle {
  display: inline-flex;
  align-items: center;
  gap: 10px;
}
.tip {
  font-size: 12px;
  white-space: nowrap;
}
</style>
