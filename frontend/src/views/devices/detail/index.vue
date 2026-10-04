<script setup>
/**
 * 节点详情。
 *
 * - 单台节点的一行现状 + 它持有的全部下标。
 * - 该节点参与的块分布（哪些块的 holder 或 replicas 含它）。
 * - 单独跑一次 PoR，只关心这一台的 asked / answered。
 */
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { devicesApi } from '../../../api/devices'
import { nodeSelfCheck, span } from '../../../utils/format'
import PageHeader from '../../../components/common/PageHeader.vue'
import BlockMatrix from '../../../components/chart/BlockMatrix.vue'
import EmptyState from '../../../components/common/EmptyState.vue'
import Icon from '../../../components/icons/Icon.vue'

const route = useRoute()
const nodeId = computed(() => route.params.id)

const loading = ref(false)
const error = ref('')
const nodes = ref([])
const porResult = ref(null)
const porRunning = ref(false)

const node = computed(() => nodes.value.find((n) => n.node_id === nodeId.value))

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await devicesApi.nodes()
    nodes.value = data
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function runPor() {
  porRunning.value = true
  porResult.value = null
  try {
    const { data } = await devicesApi.por(8)
    porResult.value = data
  } catch (e) {
    porResult.value = { ok: false, message: e?.response?.data?.detail || 'PoR 失败' }
  } finally {
    porRunning.value = false
  }
}

const myShare = computed(() => {
  if (!porResult.value?.shares) return null
  return porResult.value.shares.find((s) => s.node_id === nodeId.value)
})

onMounted(load)
</script>

<template>
  <div>
    <!-- ★ nodeId 就是 node_id（已是 node-1 这种形式），别再拼一次前缀 ——
         早先写成 `node-${nodeId}`，标题会变成 node-node-1。 -->
    <PageHeader title="节点详情" :subtitle="nodeId" />

    <div v-if="error" class="panel"><p class="text-danger">{{ error }}</p></div>
    <template v-else-if="node">
      <div class="panel mb-3">
        <h4 class="sec-title">现状</h4>
        <div class="kv">
          <div class="kv-row"><span class="k">自检</span><span :class="nodeSelfCheck(node).tone === 'ok' ? 'text-ok' : 'text-danger'">{{ nodeSelfCheck(node).label }}</span></div>
          <div class="kv-row"><span class="k">n（对账）</span><span class="mono">{{ node.unreachable ? '—' : node.n }}</span></div>
          <div class="kv-row"><span class="k">持有块数</span><span class="mono">{{ node.held }}</span></div>
          <div class="kv-row"><span class="k">跨度</span><span class="mono">{{ node.span }}</span></div>
          <div class="kv-row"><span class="k">全部下标</span><span class="mono">{{ (node.indices || []).join(', ') || '—' }}</span></div>
          <div class="kv-row"><span class="k">一致性</span><span :class="node.valid ? 'text-ok' : 'text-danger'">{{ node.valid ? '一致' : '不一致' }}</span></div>
        </div>
      </div>

      <div class="panel mb-3">
        <h4 class="sec-title">块分布矩阵</h4>
        <BlockMatrix :nodes="nodes" />
      </div>

      <div class="panel">
        <h4 class="sec-title">单独 PoR</h4>
        <el-button type="primary" :loading="porRunning" @click="runPor">
          <Icon name="pulse" :size="14" style="margin-right: 6px" />发起挑战
        </el-button>
        <div v-if="myShare" class="mt-3">
          <el-alert
            :type="myShare.unreachable ? 'error' : 'success'"
            :closable="false"
            :title="`本节点问到 ${myShare.asked} 个 / 答到 ${myShare.answered} 个`"
            :description="myShare.unreachable ? '连不上' : myShare.error || ''"
          />
        </div>
        <div v-if="porResult && !porResult.ok" class="mt-2 text-danger">{{ porResult.message }}</div>
      </div>
    </template>
    <EmptyState v-else-if="!loading" title="节点不存在" />
  </div>
</template>

<style scoped>
.sec-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 14px;
}
.kv {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.kv-row {
  display: flex;
  gap: 12px;
  align-items: baseline;
  font-size: 13px;
}
.kv-row .k {
  color: var(--text-3);
  width: 100px;
  flex-shrink: 0;
}
</style>
