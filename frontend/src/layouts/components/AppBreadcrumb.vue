<script setup>
/**
 * 顶栏面包屑。
 *
 * 层级来自 routes.js 的 meta.parent —— 只有详情类页面有父级。
 * 没有 parent 时退化成「只显示当前页名」，与原来顶栏的行为一致。
 *
 * ★ 它是可点的：详情页上「返回」原来只有一个按钮，现在父级标题本身就是入口。
 */
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

const title = computed(() => route.meta?.title || '')
const parent = computed(() => route.meta?.parent || null)

function goParent() {
  if (parent.value) router.push(parent.value.path)
}
</script>

<template>
  <nav v-if="title" class="breadcrumb" aria-label="面包屑">
    <template v-if="parent">
      <button type="button" class="parent" @click="goParent">{{ parent.title }}</button>
      <span class="sep" aria-hidden="true">/</span>
      <span class="cur">{{ title }}</span>
    </template>
    <span v-else class="cur">{{ title }}</span>
  </nav>
</template>

<style scoped>
.breadcrumb {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.parent {
  border: none;
  background: transparent;
  padding: 0;
  font-family: inherit;
  font-size: 13px;
  color: var(--text-2);
  cursor: pointer;
  white-space: nowrap;
  transition: color 0.15s ease;
}
.parent:hover {
  color: var(--accent);
}
.sep {
  color: var(--text-3);
  font-size: 13px;
}
.cur {
  font-size: 15px;
  font-weight: 500;
  color: var(--text-1);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
