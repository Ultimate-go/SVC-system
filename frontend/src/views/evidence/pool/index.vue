<script setup>
/**
 * 证据池。
 *
 * - 池子存 sessionStorage（关掉标签页就没了，刻意）。
 * - 每张卡片记录 indices / values / proof / delta_fp / delta_n / 取回时间。
 * - 按 delta_fp 判断作废（不是 n）。
 * - 「一次验这 N 份」→ verify-batch，两个耗时都显示，agree 不一致报警。
 * - 分解再聚合 4 步演示。
 * - 故障演练开关：把要发出去的那一份副本改坏一个值再验。
 */
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { usePoolStore } from '../../../stores/pool'
import { evidenceApi } from '../../../api/evidence'
import { systemApi } from '../../../api/system'
import { span, fmtAgo } from '../../../utils/format'
import PageHeader from '../../../components/common/PageHeader.vue'
import EmptyState from '../../../components/common/EmptyState.vue'
import VerifyResult from '../../../components/security/VerifyResult.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'

const pool = usePoolStore()

const batchRunning = ref(false)
const batchResult = ref(null)

const corruptToggle = ref(false)
const corruptResult = ref(null)
const corruptRunning = ref(false)

const disaggRunning = ref(false)
const disaggResult = ref(null)

const isStale = (card) => pool.staleIds.has(card.id)

function cardTitle(card) {
  return card.label || span(card.indices)
}

async function syncDelta() {
  try {
    const { data } = await systemApi.status()
    pool.syncDelta({ fp: null, n: data.delta.n })
    // 也取指纹：status 没有 delta_fp，用 query 一个空集取不到 —— 直接用 n 与已有卡比对。
    // 实际上 delta_fp 需要从某次 query 拿。这里从池子里已有的 fp 兜底。
  } catch {
    /* 忽略 */
  }
}

async function fetchOne() {
  // 取一份覆盖当前所有文件的证据（按第一个文件）
  // 简化：让用户在下标框输入。这里给一个「取 [0]」的快捷入口。
  try {
    const { data } = await evidenceApi.query([0], false)
    pool.addCard({ label: '取回：0', src: '手动取回', result: data })
    ElMessage.success('已取回一份证据')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '取回失败')
  }
}

async function batchVerify() {
  const cards = pool.selectedCards
  if (!cards.length) {
    ElMessage.warning('先勾选几张卡片')
    return
  }
  batchRunning.value = true
  batchResult.value = null
  try {
    const items = cards.map((c) => ({
      indices: c.indices,
      values: c.result.values,
      proof: c.result.proof,
    }))
    const { data } = await evidenceApi.verifyBatch(items, true, true)
    batchResult.value = data
  } catch (e) {
    batchResult.value = { ok: false, message: e?.response?.data?.detail || '批量验证失败' }
  } finally {
    batchRunning.value = false
  }
}

async function corruptVerify() {
  const cards = pool.selectedCards
  if (!cards.length) {
    ElMessage.warning('先勾选一张卡片')
    return
  }
  corruptRunning.value = true
  corruptResult.value = null
  try {
    const c = cards[0]
    // ★ 只改「要发出去的那一份副本」，不动池子、更不动服务器数据。
    const items = [
      {
        indices: c.indices,
        values: c.result.values.map((v, i) => (i === 0 ? (BigInt(v) + 1n).toString() : v)),
        proof: c.result.proof,
      },
    ]
    const { data } = await evidenceApi.verifyBatch(items, true, false)
    corruptResult.value = data
  } catch (e) {
    corruptResult.value = { ok: false, message: e?.response?.data?.detail || '演练失败' }
  } finally {
    corruptRunning.value = false
  }
}

async function runDisagg() {
  const cards = pool.selectedCards
  if (!cards.length) {
    ElMessage.warning('先勾选一张卡片（覆盖至少 2 个下标）')
    return
  }
  const c = cards[0]
  if (c.indices.length < 2) {
    ElMessage.warning('这张卡只覆盖 1 个下标，拆不出真子集')
    return
  }
  disaggRunning.value = true
  disaggResult.value = null
  try {
    const K = c.indices.slice(0, c.indices.length - 1)
    const { data } = await evidenceApi.disagg({
      I: c.indices,
      values: c.result.values,
      S_I: c.result.proof.S_I,
      Lambda_I: c.result.proof.Lambda_I,
      K,
    })
    disaggResult.value = data
    pool.addCard({ label: `拆出：${span(K)}`, src: '分解而来', result: data })
  } catch (e) {
    disaggResult.value = { ok: false, message: e?.response?.data?.detail || '分解失败' }
  } finally {
    disaggRunning.value = false
  }
}

onMounted(() => syncDelta())
</script>

<template>
  <div>
    <PageHeader title="证据池" subtitle="攒证据 → 聚合成一份（跨文件）" />

    <div class="panel mb-3">
      <div class="toolbar">
        <el-button type="primary" @click="fetchOne">取一份证据（下标 0）</el-button>
        <el-button :disabled="!pool.selectedCards.length" @click="pool.aggregateSelected()">聚合选中</el-button>
        <el-button :disabled="!pool.selectedCards.length" :loading="batchRunning" @click="batchVerify">一次验这 {{ pool.selectedCards.length }} 份</el-button>
        <el-button :disabled="!pool.selectedCards.length" @click="runDisagg" :loading="disaggRunning">分解</el-button>
        <el-button v-if="pool.cards.length" link type="danger" @click="pool.clear()">清空</el-button>
      </div>
      <p class="note">池子存 sessionStorage，关掉标签页就没了（刻意）。判断「作废」用 δ 指纹，不用 n。</p>
    </div>

    <div v-if="!pool.cards.length">
      <EmptyState title="池子是空的" description="取一份证据，或从「完整性验证」页把结果收进来" icon="list" />
    </div>

    <div v-else class="cards">
      <div v-for="card in pool.cards" :key="card.id" class="card panel" :class="{ stale: isStale(card) }">
        <el-checkbox :model-value="pool.selectedIds.includes(card.id)" @change="pool.toggle(card.id)">
          <span class="card-title mono">{{ cardTitle(card) }}</span>
        </el-checkbox>
        <div class="card-meta">
          <span class="mono text-3">取回于 n={{ card.n ?? '—' }}</span>
          <span class="mono text-3">{{ fmtAgo(card.ts) }}</span>
        </div>
        <div v-if="card.result?.verify">
          <VerifyResult :result="card.result" />
        </div>
        <el-tag v-if="isStale(card)" type="danger" size="small" class="mt-2">已作废，需重新取</el-tag>
        <div class="card-actions mt-2">
          <el-button link type="danger" size="small" @click="pool.removeCard(card.id)">移除</el-button>
        </div>
      </div>
    </div>

    <div v-if="batchResult" class="panel mt-3">
      <h4 class="sec-title">批量验证结果</h4>
      <el-alert
        :type="batchResult.ok ? 'success' : 'error'"
        :closable="false"
        :title="batchResult.message || (batchResult.ok ? '通过' : '失败')"
      />
      <div class="mono mt-2" style="font-size: 13px">
        <span>批量 {{ batchResult.ms_batch }} ms</span>
        <span class="sep">·</span>
        <span>逐份 {{ batchResult.ms_separate }} ms</span>
      </div>
      <p class="note">批量验证比逐份慢（实测约 1.68 倍）—— 它值钱的地方是「一次结论」，不是速度。</p>
      <el-alert v-if="batchResult.agree === false" type="error" :closable="false" class="mt-2" title="两套结论不一致 —— 这通常说明实现有 bug，需要显著报警" />
      <StageTimeline v-if="batchResult.timings" :timings="batchResult.timings" class="mt-2" />
    </div>

    <div class="panel mt-3">
      <h4 class="sec-title">故障演练</h4>
      <div class="flex items-center gap-3">
        <el-switch v-model="corruptToggle" />
        <span class="text-2">把要发出去的那一份副本改坏一个值再验（不动池子、更不动服务器数据）</span>
        <el-button :disabled="!corruptToggle || !pool.selectedCards.length" :loading="corruptRunning" @click="corruptVerify">演练</el-button>
      </div>
      <div v-if="corruptResult" class="mt-2">
        <el-alert :type="corruptResult.ok ? 'error' : 'error'" :closable="false" :title="corruptResult.ok ? '意外：竟然通过了' : `被抓住：${corruptResult.code_name || corruptResult.message}`" />
      </div>
    </div>

    <div v-if="disaggResult" class="panel mt-3">
      <h4 class="sec-title">分解结果</h4>
      <div class="text-2" style="font-size: 13px">
        从 {{ span(disaggResult.source_indices || []) }} 拆出 {{ span(disaggResult.indices || []) }}，
        丢弃 {{ span(disaggResult.dropped || []) }}
      </div>
      <p class="note">第 3 步不能省：K ⊆ I 是硬约束，含新下标的证据光靠这一份永远拆不出来。</p>
    </div>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.note {
  font-size: 12px;
  color: var(--text-3);
  margin-top: 8px;
}
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 12px;
}
.card.stale {
  opacity: 0.6;
}
.card-title {
  font-size: 13px;
  font-weight: 500;
}
.card-meta {
  display: flex;
  gap: 12px;
  margin: 4px 0 10px;
  font-size: 12px;
}
.sec-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 12px;
}
.sep {
  margin: 0 8px;
  color: var(--text-3);
}
</style>
