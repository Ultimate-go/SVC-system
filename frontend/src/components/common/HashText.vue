<script setup>
/**
 * 指纹显示单元：界面显示删除值，悬浮显示完整值。
 *
 * 用法与 `PubKeyFingerprint.vue` 一致（均使用原生 `title` 提示与 BigInt），
 * 本组件更通用：任何十进制的群元素均可显示。
 *
 * 不变式：界面显示的短串与提示中的完整串必须是同一字符串的前缀
 *   （两处均经 `BigInt(s).toString(16)`）。若显示十六进制而提示给十进制，
 *   两者无法对应，会被误认为显示错误。
 *   因此提示中的主串为完整十六进制，十进制仅作附加参考。
 *
 * 纯展示组件：不访问网络、不读 store，可在表格与卡片中复用。
 */
import { computed } from 'vue'
import { hexFp, hexFull, decHead } from '../../utils/format'

const props = defineProps({
  //: 十进制字符串（群元素）。后端统一以十进制传大整数。
  value: { type: [String, Number], default: '' },
  //: 界面显示的十六进制位数；提示中始终给出完整值。
  len: { type: Number, default: 16 },
  //: 提示中是否附上十进制前若干位。
  showDecimal: { type: Boolean, default: true },
  //: 前缀标签（如 "π"、"S_I"）；不传则不显示。
  label: { type: String, default: '' },
})

const short = computed(() => hexFp(props.value, props.len))
const full = computed(() => hexFull(props.value))

const tip = computed(() => {
  const s = String(props.value ?? '')
  if (!s) return '（空）'
  const parts = [`完整指纹（十六进制）：${full.value}`]
  if (props.showDecimal) parts.push(`十进制：${decHead(s, 24)}`)
  parts.push(`长度：${s.length} 位十进制 / ${full.value.length} 位十六进制`)
  return parts.join('\n')
})
</script>

<template>
  <span class="hash-text mono" :title="tip">
    <span v-if="label" class="lbl">{{ label }}</span>{{ short }}
  </span>
</template>

<style scoped>
.hash-text {
  font-size: 13px;
  color: var(--accent);
  cursor: help;
  /* 提示中给出完整值，因此短串允许删除显示。 */
  word-break: break-all;
}
.hash-text .lbl {
  color: var(--text-3);
  margin-right: 4px;
}
</style>
