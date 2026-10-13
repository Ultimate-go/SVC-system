<script setup>
/**
 * 顶栏面包屑。
 *
 * 层级来自 routes.js 的 meta.parent —— 只有详情类页面有父级。
 * 无 parent 时退化为只显示当前页名。
 *
 * 父级标题本身可点击，作为详情页的返回入口。
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
