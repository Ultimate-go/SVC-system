<script setup>
/**
 * 查询区：关键词输入 + 若干下拉 + 「查询」+「重置」。回车即查。
 * 槽位放筛选下拉；v-model:keyword 绑定关键词。
 */
const props = defineProps({
  keyword: { type: String, default: '' },
  placeholder: { type: String, default: '搜索…' },
  // ★ 用户管理页不需要「重置」筛选按钮（管理员容易误以为是「重置口令」），
  //   默认显示，按需关闭。
  showReset: { type: Boolean, default: true },
})
const emit = defineEmits(['update:keyword', 'search', 'reset'])
</script>

<template>
  <div class="filter-bar panel">
    <el-input
      :model-value="keyword"
      :placeholder="placeholder"
      clearable
      class="keyword"
      @update:model-value="emit('update:keyword', $event)"
      @keyup.enter="emit('search')"
    />
    <slot />
    <el-button type="primary" @click="emit('search')">查询</el-button>
    <el-button v-if="showReset" @click="emit('reset')">重置</el-button>
  </div>
</template>

<style scoped>
.filter-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.keyword {
  width: 240px;
}
</style>
