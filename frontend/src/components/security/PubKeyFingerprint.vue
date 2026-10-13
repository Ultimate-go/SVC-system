<script setup>
/**
 * 公钥指纹。使用 hexFp（内部 BigInt）；title 给出十进制前 24 位与总位数。
 * 大整数不得经 Number() 转换。
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
  font-size: 13px;
}
.fp {
  color: var(--accent);
}
.bits {
  color: var(--text-3);
  font-size: 13px;
}
</style>
