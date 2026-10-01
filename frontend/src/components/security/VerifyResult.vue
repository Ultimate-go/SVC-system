<script setup>
/**
 * 验证结果 —— 结论 + 失败环节名。
 *
 * ★ 只有一个结论：``ok`` 就是承诺验证（SVC）的结论。而承诺里那些分量，是
 *   验证方**自己从收到的密文算**出来的（见 core/store.py::_collect）——
 *   所以这一个结论已经把"交付的字节对不对得上承诺"一并盖住了，没有第二层可看。
 *   标题绝不能写成「验证失败 · OK」。直接用 verifyFailTitle / verifyFailDetail。
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
        <span class="layer-name">向量承诺验证（唯一结论）</span>
        <span class="layer-state">{{ verifyOk ? '通过' : `失败 · ${codeName}` }}</span>
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
