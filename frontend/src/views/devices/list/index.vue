<script setup>
/**
 * 设备（存储节点）列表。
 *
 * - StatCard 行：节点总数 / 在线数 / 离线列表 / 副本数 / 副本不足块数。
 * - 节点表格：node_id / n / held / span / 一致性(valid) / 本地自检(nodeSelfCheck) / db。
 * - 块分布矩阵（BlockMatrix）：行=全局下标，列=节点。
 * - PoR 挑战卡：lambda_pos 可调 → POST /api/por，逐台 asked:answered。
 * - 待补推横幅 + 补推按钮（仅管理员）。
 */
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { devicesApi } from '../../../api/devices'
import { systemApi } from '../../../api/system'
import { usePermission } from '../../../composables/usePermission'
import { nodeSelfCheck } from '../../../utils/format'
import PageHeader from '../../../components/common/PageHeader.vue'
import StatCard from '../../../components/common/StatCard.vue'
import BlockMatrix from '../../../components/chart/BlockMatrix.vue'
import EmptyState from '../../../components/common/EmptyState.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'
import Icon from '../../../components/icons/Icon.vue'

const { isAdmin } = usePermission()

const loading = ref(false)
const error = ref('')
const nodes = ref([])
const status = ref(null)
const pending = ref(null)

const lambdaPos = ref(8)
const porRunning = ref(false)
const porResult = ref(null)

const retrying = ref(false)

const onlineCount = computed(() => nodes.value.filter((n) => !n.unreachable && !n.fresh).length)

const hasPending = computed(() => !!(pending.value?.pending || pending.value?.persist_pending))

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [n, s, p] = await Promise.all([
      devicesApi.nodes(),
      systemApi.status(),
      devicesApi.pending(),
    ])
    nodes.value = n.data
    status.value = s.data
    pending.value = p.data
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
    const { data } = await devicesApi.por(lambdaPos.value)
    porResult.value = data
  } catch (e) {
    porResult.value = { ok: false, message: e?.response?.data?.detail || 'PoR 失败' }
  } finally {
    porRunning.value = false
  }
}

async function retryPush() {
  retrying.value = true
  try {
    const { data } = await devicesApi.retryPush()
    if (data.ok) ElMessage.success('补推完成，全网已收敛')
    else ElMessage.warning('补推了，但还有几台没通 —— 把机器弄活再点一次')
    await load()
  } catch {
    // 错误已由拦截器弹出
  } finally {
    retrying.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <PageHeader title="设备（存储节点）" subtitle="一台服务器 = 一个独立进程 + 一个独立 SQLite">
      <el-button @click="load"><Icon name="refresh" :size="14" style="margin-right: 6px" />刷新</el-button>
    </PageHeader>

    <el-alert
      v-if="hasPending"
      type="warning"
      :closable="false"
      class="mb-3"
      title="有写推未完成"
      description="不要重新上传 —— 登记表已经分配过那批下标，重传会拿到新下标。把没通的机器弄活，点「补推」即可收敛。"
    >
      <template #default>
        <el-button v-if="isAdmin" size="small" :loading="retrying" @click="retryPush">补推</el-button>
      </template>
    </el-alert>

    <div v-if="error" class="panel"><p class="text-danger">{{ error }}</p></div>
    <template v-else>
      <div class="grid mb-3">
        <StatCard label="节点总数" :value="nodes.length" />
        <StatCard label="在线" :value="onlineCount" />
        <StatCard label="副本数" :value="status?.replica_factor || '—'" />
        <StatCard label="副本不足块数" :value="status?.under_replicated || 0" />
      </div>

      <div class="panel mb-3">
        <h4 class="sec-title">节点现状</h4>
        <el-table :data="nodes" v-loading="loading" border>
          <el-table-column prop="node_id" label="节点" width="100">
            <template #default="{ row }"><span class="mono">{{ row.node_id }}</span></template>
          </el-table-column>
          <el-table-column prop="n" label="n（对账）" width="90" align="center">
            <template #default="{ row }"><span class="mono">{{ row.unreachable ? '—' : row.n }}</span></template>
          </el-table-column>
          <el-table-column prop="held" label="持有块数" width="90" align="center" />
          <el-table-column prop="span" label="跨度 span" min-width="180">
            <template #default="{ row }"><span class="mono">{{ row.span }}</span></template>
          </el-table-column>
          <el-table-column label="一致性" width="90" align="center">
            <template #default="{ row }">
              <span :class="row.valid ? 'text-ok' : 'text-danger'">{{ row.valid ? '一致' : '不一致' }}</span>
            </template>
          </el-table-column>
          <el-table-column label="本地自检" width="110" align="center">
            <template #default="{ row }">
              <span :class="`text-${nodeSelfCheck(row).tone === 'ok' ? 'ok' : nodeSelfCheck(row).tone === 'danger' ? 'danger' : 'warn'}`">
                {{ nodeSelfCheck(row).label }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="数据库" min-width="180">
            <template #default="{ row }">
              <span v-if="row.db" class="mono text-2" style="font-size: 12px">
                {{ row.db.blobs ?? '' }} 块 / {{ row.db.state ?? '' }} 状态
              </span>
              <span v-else class="text-3">—</span>
            </template>
          </el-table-column>
          <template #empty>
            <EmptyState title="没有节点" />
          </template>
        </el-table>
      </div>

      <div class="panel mb-3">
        <h4 class="sec-title">块分布矩阵（行 = 全局下标，列 = 节点）</h4>
        <BlockMatrix :nodes="nodes" />
      </div>

      <div class="panel mb-3">
        <h4 class="sec-title">存储证明（PoR）挑战</h4>
        <p class="note">
          它问的是「你还在存着吗」，与完整性验证问的「对不对」不是同一件事；而且它一个字节的内容都不取。
        </p>
        <div class="por-control">
          <span>挑战位置数 λ_pos：</span>
          <el-input-number v-model="lambdaPos" :min="1" :max="256" />
          <el-button type="primary" :loading="porRunning" @click="runPor">发起挑战</el-button>
        </div>

        <div v-if="porResult" class="mt-3">
          <el-alert
            :type="porResult.ok ? 'success' : 'error'"
            :closable="false"
            :title="`结论：${porResult.verdict || (porResult.ok ? '通过' : '不通过')}`"
            :description="porResult.message || ''"
          />
          <div class="por-stats mono mt-2">
            <span>点名 {{ porResult.asked_total }} 个 / 答齐 {{ porResult.answered_total }} 个</span>
            <span>证据 {{ porResult.proof_size_bytes }} 字节</span>
            <span>合并份额 {{ porResult.aggregated_shares }}</span>
          </div>
          <el-table :data="porResult.shares || []" size="small" class="mt-2">
            <el-table-column prop="node_id" label="节点" width="100" />
            <el-table-column label="问到 / 答到" width="140" align="center">
              <template #default="{ row }">
                <span class="mono">{{ row.asked }} / {{ row.answered }}</span>
              </template>
            </el-table-column>
            <el-table-column label="状态">
              <template #default="{ row }">
                <span v-if="row.unreachable" class="text-danger">连不上</span>
                <span v-else-if="row.error" class="text-danger">{{ row.error }}</span>
                <span v-else class="text-ok">已答</span>
              </template>
            </el-table-column>
          </el-table>
          <StageTimeline v-if="porResult.timings" :timings="porResult.timings" class="mt-3" />
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 12px;
}
.sec-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 14px;
}
.note {
  font-size: 12px;
  color: var(--text-3);
  margin-bottom: 12px;
}
.por-control {
  display: flex;
  align-items: center;
  gap: 12px;
}
.por-stats {
  display: flex;
  gap: 20px;
  font-size: 13px;
  color: var(--text-1);
}
</style>
