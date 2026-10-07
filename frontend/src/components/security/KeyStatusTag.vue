<script setup>
/**
 * 密钥对状态标签。
 *
 * ★ 默认模型（私钥在客户端）下它必须分成**三件事**，之前把它们混在
 *   `session_key` 一个字段上，于是顶栏会显示「需重新登录」——
 *   而那时候浏览器其实已经解锁了。界面与现实不符是硬伤，所以改这里。
 *
 *  - `has_key`     库级：库里有没有密钥对
 *  - `session_key` **服务端**能不能解密。
 *      默认模型下它**恒为 false**（后端根本没有私钥），
 *      所以它不再等于“能不能解密”，不能再拿它渲染“需重新登录”。
 *  - `unlocked`    **浏览器**这边有没有私钥（`stores/crypto`，只活在内存里）。
 *      这才是“我现在能不能解密”的答案。
 */
import { computed } from 'vue'
import { useCryptoStore } from '../../stores/crypto'

const props = defineProps({
  user: { type: Object, default: null },
})

const crypt = useCryptoStore()

const hasKey = computed(() => !!props.user?.has_key)
const unlocked = computed(() => crypt.unlocked)
const serverHoldsKey = computed(() => props.user?.session_key === true)
</script>

<template>
  <div class="key-status">
    <span class="tag" :class="hasKey ? 'ok' : 'danger'">
      {{ hasKey ? '有密钥对' : '无密钥对' }}
    </span>
    <span class="tag" :class="unlocked ? 'ok' : 'warn'">
      {{ unlocked ? '已解锁 · 私钥在本浏览器' : '未解锁 · 重新登录' }}
    </span>
    <span v-if="serverHoldsKey" class="tag warn" title="旧模型（登录时传了 server_key=true）：私钥也在服务端内存里">
      服务端也持有私钥
    </span>
    <span v-else class="tag muted" title="默认模型：后端不解封、不保存私钥；解密与验证都在浏览器里">
      服务端无私钥
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
