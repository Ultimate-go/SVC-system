<script setup>
/**
 * 密钥对状态标签 —— has_key 与 session_key 是两件事，必须分开显示。
 *
 *  - has_key    库级：库里有没有密钥对（后端重启/退出后仍是 true）
 *  - session_key 会话级：本次会话能不能解密（后端重启/退出后变 false）
 *
 * 只显示 has_key 的话，页面看着全正常，一点解密才弹「重新登录」。
 * 所以顶栏 / 个人中心用本组件同时挂两枚标签。
 */
import { computed } from 'vue'

const props = defineProps({
  user: { type: Object, default: null },
})

const hasKey = computed(() => !!props.user?.has_key)
const sessionKey = computed(() => props.user?.session_key)
</script>

<template>
  <div class="key-status">
    <span class="tag" :class="hasKey ? 'ok' : 'danger'">
      {{ hasKey ? '有密钥对' : '无密钥对' }}
    </span>
    <span class="tag" :class="sessionKey === true ? 'ok' : sessionKey === false ? 'warn' : 'muted'">
      <template v-if="sessionKey === true">会话私钥在</template>
      <template v-else-if="sessionKey === false">需重新登录</template>
      <template v-else>会话未查</template>
    </span>
  </div>
</template>

<style scoped>
.key-status {
  display: inline-flex;
  gap: 6px;
  align-items: center;
}
.tag {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--line-strong);
  line-height: 1.5;
  white-space: nowrap;
}
.tag.ok {
  color: var(--ok);
  border-color: var(--ok);
}
.tag.danger {
  color: var(--danger);
  border-color: var(--danger);
}
.tag.warn {
  color: var(--warn);
  border-color: var(--warn);
}
.tag.muted {
  color: var(--text-3);
}
</style>
