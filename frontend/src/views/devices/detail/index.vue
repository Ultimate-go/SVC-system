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
        <h4 class="sec-title">存储证明（PoR）挑战</h4>
        <!-- ★ 这块以前只有一句标题 + 一个按钮，**没有任何说明** —— 用户不知道
             “挑战”是什么、看到的结果意味着什么。补上四句：它问什么、怎么算过、
             数字怎么读、以及它**不能**证明什么。 -->
        <p class="por-note">
          PoR（Proof of Retrievability，可检索性证明）验的是
          <b>这台节点是不是真的还存着它名下的那些块</b> —— 不是听它自己说，
          而是让它现场回答一份挑战。
        </p>
        <p class="por-note">
          流程：协调者随机抽一批块 → 让节点交出这些块的密文 →
          协调者<b>自己重算</b>分量（<code class="mono">v = SM3(密文)</code>）
          并与登记表里的记录比对：一致则记为已响应，不一致或不可达则记为未响应。
          所以「问到 N / 答到 M」里 <b>M &lt; N</b> 的含义很明确：<b>它答不上来</b>。
        </p>
        <p class="por-note">
          ★ 诚实边界：这只是<b>抽样</b>，不是全量盘点 —— 答到 N 个只说明
          “抽查到的这些它确实都在”，<b>不能</b>推出“它一块都没丢”。
          要更强的不在树里的保证，得看上面的块分布矩阵与「全量自检」。
        </p>
        <el-button type="primary" :loading="porRunning" @click="runPor">
          <Icon name="pulse" :size="14" style="margin-right: 6px" />发起挑战
        </el-button>
        <div v-if="myShare" class="mt-3">
          <el-alert
            :type="myShare.unreachable ? 'error' : 'success'"
            :closable="false"
            :title="`本节点问到 ${myShare.asked} 个 / 答到 ${myShare.answered} 个`"
            :description="myShare.unreachable ? '不可达' : myShare.error || ''"
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
/* PoR 那块的四段说明（安全审计之后补的“这按钮到底在干什么”） */
.por-note {
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--text-3);
  margin: 0 0 8px;
  max-width: 76ch;
}
.por-note b {
  color: var(--text-2);
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
