<script setup>
/**
 * 验证结果 —— 结论 + 失败环节名。
 *
 * ★ 两层必须分开显示：
 *   - verify.ok         承诺层（分量对不对）
 *   - hash_layer_ok     块哈希层（密文与分量对不对得上）
 *
 * 存在「承诺层过了、块哈希层挂了」的情形（密文被换、分量没动），
 * 标题绝不能写成「验证失败 · OK」。直接用 verifyFailTitle / verifyFailDetail。
 */
import { computed } from 'vue'
import { verifyFailTitle, verifyFailDetail } from '../../utils/format'
import Icon from '../icons/Icon.vue'

const props = defineProps({
  result: { type: Object, default: null },
})

const title = computed(() => verifyFailTitle(props.result))
const detail = computed(() => verifyFailDetail(props.result))
const overallOk = computed(() => !!props.result?.ok)

const verifyOk = computed(() => props.result?.verify?.ok === true)
const hashOk = computed(() => props.result?.hash_layer_ok === true)
const codeName = computed(() => props.result?.verify?.code_name || '')
</script>

<template>
  <div class="verify-result" v-if="result">
    <div class="headline" :class="overallOk ? 'ok' : 'danger'">
      <Icon :name="overallOk ? 'check' : 'x'" :size="18" />
      <span class="title">{{ title }}</span>
    </div>

    <div class="layers">
      <div class="layer" :class="verifyOk ? 'ok' : 'danger'">
        <span class="layer-name">① 向量承诺层</span>
        <span class="layer-state">{{ verifyOk ? '通过' : `失败 · ${codeName}` }}</span>
      </div>
      <div class="layer" :class="hashOk ? 'ok' : 'danger'">
        <span class="layer-name">② 块哈希层</span>
        <span class="layer-state">{{ hashOk ? '通过' : '失败' }}</span>
      </div>
    </div>

    <div class="detail" v-if="detail">{{ detail }}</div>
  </div>
</template>

<style scoped>
.verify-result {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.headline {
  display: flex;
  align-items: center;
  gap: 8px;
  font-weight: 500;
}
.headline.ok { color: var(--ok); }
.headline.danger { color: var(--danger); }
.layers {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.layer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--bg-raised);
  font-size: 13px;
}
.layer.ok { border-color: var(--ok); }
.layer.danger { border-color: var(--danger); }
.layer-state {
  font-family: var(--font-mono);
  font-size: 12px;
}
.detail {
  font-size: 13px;
  color: var(--text-2);
}
</style>
