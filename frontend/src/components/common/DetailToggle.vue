<script setup>
/**
 * 「简略 / 详细」开关：控制是否逐块显示指纹。
 *
 * 该状态属于界面偏好，与主题、密度一致，存于 `stores/theme.js` → localStorage。
 * 在同一页切到「详细」后，切换到其它页面应保持「详细」。
 * 默认值为「详细」。
 */
import { computed } from 'vue'
import { useThemeStore } from '../../stores/theme'

const theme = useThemeStore()
const mode = computed(() => theme.detailMode)
</script>

<template>
  <div class="detail-toggle">
    <span class="tip text-3">{{ mode === 'detail' ? '显示每块指纹（悬浮查看完整值）' : '指纹已折叠，仅显示块号、长度与持有节点' }}</span>
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
  font-size: 13px;
  white-space: nowrap;
}
</style>
