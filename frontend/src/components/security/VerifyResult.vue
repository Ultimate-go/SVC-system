<script setup>
/**
 * 验证结果：结论与失败环节名。
 *
 * 只有一个结论：``ok`` 即承诺验证（SVC）的结论。承诺中的分量由验证方
 *   依据收到的密文自行计算（见 core/store.py::_collect），
 *   因此该结论已覆盖「交付字节是否与承诺一致」这一判定，不存在第二层。
 *   标题不得写成「验证失败 · OK」；失败时使用 verifyFailTitle / verifyFailDetail。
 */
import { computed } from 'vue'
import { verifyFailTitle, verifyFailDetail } from '../../utils/format'
import Icon from '../icons/Icon.vue'

const props = defineProps({
  result: { type: Object, default: null },
  /**
   * 是否在结论下面再列一行 detail。
   *
   * ★ 成功时后端那句 message 就是“验证通过”，与上面的标题完全重复 —— 调用方
   *   可以传 false 省掉；失败时那句是“哪一步没过”，有用，调用方仍传 true。
   */
  showDetail: { type: Boolean, default: true },
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

    <div class="detail" v-if="showDetail && detail">{{ detail }}</div>
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
  font-size: 13px;
}
.detail {
  font-size: 13px;
  color: var(--text-2);
}
</style>
