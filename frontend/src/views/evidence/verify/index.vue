<script setup>
/**
 * 完整性验证（验证不受限）。
 *
 * - 输入全局下标（支持 0-3, 8 区间语法）+ 允许部分结果开关。
 * - 展示两层结论（承诺层 / 块哈希层）、code_name、proof.size_bytes、holders、refs、missing。
 * - 另给「按文件查」（一份证据横跨多文件）与「按全局下标查登记表」两个入口。
 */
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { evidenceApi } from '../../../api/evidence'
import { usePoolStore } from '../../../stores/pool'
import { parseIndexRange } from '../../../utils/validate'
import PageHeader from '../../../components/common/PageHeader.vue'
import VerifyResult from '../../../components/security/VerifyResult.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'

const pool = usePoolStore()

const indicesInput = ref('0-3, 8')
const allowPartial = ref(false)
const running = ref(false)
const result = ref(null)

const fileOwner = ref('')
const fileKey = ref('')
const fileQueryRunning = ref(false)
const fileQueryResult = ref(null)

const regIndex = ref('')
const regResult = ref(null)
const regRunning = ref(false)

async function runQuery() {
  let indices
  try {
    indices = parseIndexRange(indicesInput.value)
  } catch (e) {
    ElMessage.warning(e.message)
    return
  }
  if (!indices.length) {
    ElMessage.warning('请输入下标')
    return
  }
  running.value = true
  result.value = null
  try {
    const { data } = await evidenceApi.query(indices, allowPartial.value)
    result.value = data
    // 收进证据池
    pool.addCard({ label: `查询：${indices.join(',')}`, src: '完整性验证', result: data })
  } catch (e) {
    result.value = { ok: false, verify: { ok: false, code_name: '', message: e?.response?.data?.detail || '查询失败' } }
  } finally {
    running.value = false
  }
}

async function runFileQuery() {
  if (!fileOwner.value.trim() || !fileKey.value.trim()) {
    ElMessage.warning('请输入所有者与文件标识')
    return
  }
  fileQueryRunning.value = true
  fileQueryResult.value = null
  try {
    const { data } = await evidenceApi.queryFiles([[fileOwner.value.trim(), fileKey.value.trim()]], null)
    fileQueryResult.value = data
    pool.addCard({ label: `${fileOwner.value}/${fileKey.value}`, src: '按文件查', result: data })
  } catch (e) {
    fileQueryResult.value = { ok: false, verify: { ok: false, code_name: '', message: e?.response?.data?.detail || '查询失败' } }
  } finally {
    fileQueryRunning.value = false
  }
}

async function runRegistry() {
  const i = Number(regIndex.value)
  if (!Number.isInteger(i) || i < 0) {
    ElMessage.warning('请输入非负整数下标')
    return
  }
  regRunning.value = true
  regResult.value = null
  try {
    const { data } = await evidenceApi.registry(i)
    regResult.value = data
  } catch (e) {
    regResult.value = { error: e?.response?.data?.detail || '查不到' }
  } finally {
    regRunning.value = false
  }
}
</script>

<template>
  <div>
    <PageHeader title="完整性验证" subtitle="验证不受限 —— 登录即可，不做任何权限判断" />

    <div class="panel mb-3">
      <h4 class="sec-title">按全局下标验证</h4>
      <div class="query-form">
        <el-input v-model="indicesInput" placeholder="如 0-3, 8" style="width: 260px" @keyup.enter="runQuery" />
        <el-switch v-model="allowPartial" active-text="允许部分结果" />
        <el-button type="primary" :loading="running" @click="runQuery">验证</el-button>
      </div>

      <div v-if="result" class="mt-3">
        <VerifyResult :result="result" />
        <div v-if="result.missing?.length" class="missing">
          <el-alert type="warning" :closable="false" :title="`这份结论没覆盖：${result.missing.join(', ')}`" />
        </div>
        <div v-if="result.proof" class="mono text-2 mt-2" style="font-size: 12px">
          证据 {{ result.proof.size_bytes }} 字节（与打开多少块无关）
        </div>
        <div v-if="result.refs?.length" class="refs mt-2">
          <div v-for="r in result.refs" :key="r.global_index" class="ref-row">
            <span class="mono">{{ r.global_index }}</span>
            <span class="text-2">→ {{ r.owner }} / {{ r.file_key }} 的第 {{ r.block_idx }} 块</span>
            <span class="mono text-3">holder: {{ result.holders?.[r.global_index] }}</span>
          </div>
        </div>
        <StageTimeline v-if="result.timings" :timings="result.timings" class="mt-2" />
      </div>
    </div>

    <div class="panel mb-3">
      <h4 class="sec-title">按文件查（一份证据横跨多个文件、多个用户）</h4>
      <div class="query-form">
        <el-input v-model="fileOwner" placeholder="所有者" style="width: 140px" />
        <el-input v-model="fileKey" placeholder="文件标识" style="width: 180px" />
        <el-button type="primary" :loading="fileQueryRunning" @click="runFileQuery">查询</el-button>
      </div>
      <div v-if="fileQueryResult" class="mt-3">
        <VerifyResult :result="fileQueryResult" />
      </div>
    </div>

    <div class="panel">
      <h4 class="sec-title">查登记表（全局下标 → 谁的第几块、存在哪台）</h4>
      <div class="query-form">
        <el-input v-model="regIndex" placeholder="全局下标，如 3" style="width: 160px" />
        <el-button type="primary" :loading="regRunning" @click="runRegistry">查询</el-button>
      </div>
      <div v-if="regResult" class="mt-3">
        <template v-if="!regResult.error">
          <div class="mono text-1" style="font-size: 13px">
            下标 {{ regResult.global_index }} → {{ regResult.owner }} / {{ regResult.file_key }} 的第 {{ regResult.block_idx }} 块
          </div>
          <div class="text-2 mono" style="font-size: 12px">
            holder: {{ regResult.holder }} · replicas: {{ (regResult.replicas || []).join(', ') }}
          </div>
        </template>
        <div v-else class="text-danger">{{ regResult.error }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sec-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 12px;
}
.query-form {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
.missing {
  margin-top: 10px;
}
.refs {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}
.ref-row {
  display: flex;
  gap: 10px;
  align-items: baseline;
}
</style>
