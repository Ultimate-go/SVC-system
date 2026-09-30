<script setup>
/**
 * 一个「指纹」显示单元：**界面显示截断值，悬浮出完整值**。
 *
 * 用法与 `PubKeyFingerprint.vue` 同一套（都是 `title` 原生提示、都走 BigInt），
 * 区别是这个更通用：任何一个十进制的群元素都能用它显示。
 *
 * ★★ 一条不能反的约定：**显示的短串与提示里的完整串必须是同一个字符串的前缀**
 *   （两处都走 `BigInt(s).toString(16)`）。曾经有过"界面显示十六进制前 8 位、
 *   悬浮给十进制前 24 位"的写法 —— 那两者一眼对不上号，人会以为自己看错了。
 *   所以提示里主串是**完整十六进制**，十进制只作为附加参考。
 *
 * ★ 不碰网络、不读 store：纯展示件，方便在表格/卡片里到处放。
 */
import { computed } from 'vue'
import { hexFp, hexFull, decHead } from '../../utils/format'

const props = defineProps({
  //: 十进制字符串（群元素）—— 后端一律按十进制传大整数。
  value: { type: [String, Number], default: '' },
  //: 界面上显示多少位十六进制（提示里给的永远是完整值）。
  len: { type: Number, default: 16 },
  //: 提示里是否附上十进制前若干位（老读者习惯用十进制认东西）。
  showDecimal: { type: Boolean, default: true },
  //: 前缀标签（比如 "π"、"S_I"），不传就不显示。
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
  font-size: 12px;
  color: var(--accent);
  cursor: help;
  /* 提示里给完整值，所以短串本身允许被截断显示 */
  word-break: break-all;
}
.hash-text .lbl {
  color: var(--text-3);
  margin-right: 4px;
}
</style>
