<script setup>
/**
 * 公钥指纹 —— 走 hexFp（内部 BigInt），title 给十进制前 24 位 + 总位数。
 * 大整数绝不过 Number()。
 */
import { computed } from 'vue'
import { hexFp, decHead } from '../../utils/format'

const props = defineProps({
  pubKey: { type: String, default: '' },
})

const fp = computed(() => hexFp(props.pubKey))
const full = computed(() => (props.pubKey ? decHead(props.pubKey, 24) : ''))
</script>

<template>
  <span class="pubkey mono" :title="full">
    <span class="fp">{{ fp }}</span>
    <span class="bits">{{ pubKey ? `${String(pubKey).length} 位` : '' }}</span>
  </span>
</template>

<style scoped>
.pubkey {
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
  font-size: 12px;
}
.fp {
  color: var(--accent);
}
.bits {
  color: var(--text-3);
  font-size: 11px;
}
</style>
