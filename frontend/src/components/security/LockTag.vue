<script setup>
/**
 * 锁标签：表示当前用户能否解密。
 *
 * 判据只有一条：是否为所有者（can_decrypt）。管理员不例外。
 *   本标签与文件列表中的「验证锁」不同：验证锁对所有用户均可见。
 */
import Icon from '../icons/Icon.vue'

const props = defineProps({
  canDecrypt: { type: Boolean, default: false },
})

const label = props.canDecrypt ? '能解密' : '不能解密'
</script>

<template>
  <span class="lock-tag" :class="canDecrypt ? 'ok' : 'danger'" :title="label">
    <Icon :name="canDecrypt ? 'unlock' : 'lock'" :size="13" />
    <span>{{ label }}</span>
  </span>
</template>

<style scoped>
.lock-tag {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  padding: 2px 8px;
  border-radius: 999px;
  border: 1px solid var(--line-strong);
  white-space: nowrap;
}
.lock-tag.ok {
  color: var(--ok);
  border-color: var(--ok);
}
.lock-tag.danger {
  color: var(--danger);
  border-color: var(--danger);
}
</style>
