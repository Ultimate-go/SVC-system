<script setup>
/**
 * 存储节点列表。
 *
 * - StatCard 行：节点总数 / 在线数 / 离线列表 / 副本数 / 副本不足块数。
 * - 节点表格：node_id / n / held / span / 一致性(valid) / 本地自检(nodeSelfCheck) / db。
 * - 块分布矩阵（BlockMatrix）：行=存储槽位，列=节点。
 * - PoR 挑战卡：lambda_pos 可调 → POST /api/por，逐台 asked:answered。
 * - 存储节点列表与连接状态。
 * - **服务器台数卡片（仅管理员）**：改台数 → 保存 → 「立刻重启」。
 *   它是这一页最上面那块，因为"几台机器"就是这一页在讲的东西。
 * - **端口卡片（仅管理员）**：后端 / 前端 / 每一台节点各自的端口 ——
 *   与台数同一套模型（改 → 保存 → 重启生效），紧挨着台数卡。
 */
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { devicesApi } from '../../../api/devices'
import { systemApi } from '../../../api/system'
import { usePermission } from '../../../composables/usePermission'
import { nodeSelfCheck } from '../../../utils/format'
import PageHeader from '../../../components/common/PageHeader.vue'
import StatCard from '../../../components/common/StatCard.vue'
import NodeCountCard from '../../../components/common/NodeCountCard.vue'
import PortsCard from '../../../components/common/PortsCard.vue'
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

/** 两张卡片各自管自己的加载；「刷新」时一起刷一下它们。 */
const deployCard = ref(null)
const portsCard = ref(null)

const lambdaPos = ref(8)
const porRunning = ref(false)
const porResult = ref(null)
/** PoR 挑战的端到端耗时（毫秒）：从点下按钮到挑战返回。 */
const porElapsed = ref(0)

// ★ 故障演练（仅管理员）。
//   ``drill`` 是后端返回的“现状 + 影响面”，不是前端自己推的 ——
//   “哪些块已经取不到了”只有协调者算得出来（它手里有副本表）。
const drill = ref(null)
const drillNodes = ref([])
const drillRunning = ref(false)
const drillFaulty = computed(() => new Set(drill.value?.faulty || []))
const drillDestroyed = computed(() => new Set(drill.value?.destroyed || []))

// ★ 「在线」= **答上话**的台数（``/api/nodes`` 返回 unreachable 就表示连不上）。
//   早先还多要求一个 ``!n.fresh`` —— 而 ``fresh`` 的意思是"这台机器上
//   什么都没有"，空库时**每一台**都是 fresh，于是卡片会写「在线 0」，
//   同时下面又写着「运行中 4 台」，自相矛盾（与 ``/api/status`` 的
//   ``nodes`` 是同一类错误，那边已按“可达”来数）。
const onlineCount = computed(() => nodes.value.filter((n) => !n.unreachable).length)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [n, s] = await Promise.all([
      devicesApi.nodes(),
      systemApi.status(),
    ])
    nodes.value = n.data
    status.value = s.data
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function runPor() {
  porRunning.value = true
  porResult.value = null
  porElapsed.value = 0
  const t0 = performance.now()
  try {
    const { data } = await devicesApi.por(lambdaPos.value)
    porResult.value = data
    porElapsed.value = Math.round(performance.now() - t0)
  } catch (e) {
    porResult.value = { ok: false, message: e?.response?.data?.detail || 'PoR 失败' }
  } finally {
    porRunning.value = false
  }
}

/** 拉一次演练现状 + 影响面（仅管理员；普通用户打这个接口是 403）。 */
async function loadDrill() {
  if (!isAdmin.value) return
  try {
    const { data } = await devicesApi.faultStatus()
    drill.value = data
  } catch {
    // 拦截器已提示；演练状态拿不到不该把整页卡住
  }
}

/**
 * 发动 / 撤销一次演练。
 *
 * @param action ``knock_out`` | ``restore``
 * @param mode   ``down``（掉线，可恢复）| ``destroyed``（永久损毁，不可逆）
 */
async function runDrill(action, mode = 'down') {
  const body = { action, mode }
  if (action === 'knock_out') {
    if (!drillNodes.value.length) {
      ElMessage.warning('请先勾选要模拟故障的节点')
      return
    }
    body.nodes = [...drillNodes.value]
  }
  drillRunning.value = true
  try {
    const { data } = await devicesApi.faultDrill(body)
    drill.value = data
    drillNodes.value = []
    if (action === 'restore') {
      ElMessage.success('已撤销模拟：全部节点恢复在线')
    } else {
      ElMessage.warning(
        mode === 'destroyed'
          ? '已模拟永久损毁（不可逆），影响范围见下方；可在「完整性验证」或「更新文件」页继续观察'
          : '已模拟掉线（可恢复），影响范围见下方；可在「完整性验证」或「更新文件」页继续观察',
      )
    }
    await load()
  } catch {
    // 错误已由拦截器弹出
  } finally {
    drillRunning.value = false
  }
}

onMounted(() => {
  load()
  loadDrill()
})

/** 页头那个「刷新」：本页的节点现状 + 两张部署卡片一起刷。 */
async function refreshAll() {
  await load()
  await loadDrill()
  deployCard.value?.refresh?.()
  portsCard.value?.refresh?.()
}
</script>

<template>
  <div>
    <PageHeader title="存储节点" subtitle="每台存储节点为一个独立进程，各自持有一份 SQLite 数据库">
      <el-button @click="refreshAll"><Icon name="refresh" :size="14" style="margin-right: 6px" />刷新</el-button>
    </PageHeader>

    <!-- ★ 仅管理员：改集群规模。放在最上面 —— 这个页面在讲的就是"几台机器"。 -->
    <NodeCountCard v-if="isAdmin" ref="deployCard" />

    <!-- ★ 紧跟台数卡："几台"与"各自在哪个端口"是同一件事的两面，
         改台数之后端口那几行也要跟着变（两张卡片靠 deployBus 通信）。 -->
    <PortsCard v-if="isAdmin" ref="portsCard" />

    <div v-if="error" class="panel"><p class="text-danger">{{ error }}</p></div>
    <template v-else>
      <!-- ★ 仅管理员：故障演练。放在节点现状之前 —— 它就是“让这些机器出事”。 -->
      <div v-if="isAdmin" class="panel mb-3">
        <h4 class="sec-title">故障演练（模拟节点离线 / 永久损毁）</h4>
        <el-checkbox-group v-model="drillNodes" class="drill-pick">
          <el-checkbox v-for="n in nodes" :key="n.node_id" :value="n.node_id">
            <span class="mono">{{ n.node_id }}</span>
            <el-tag
              v-if="drillFaulty.has(n.node_id)"
              size="small"
              :type="drillDestroyed.has(n.node_id) ? 'danger' : 'warning'"
              effect="plain"
              style="margin-left: 6px"
            >{{ drillDestroyed.has(n.node_id) ? '已损毁' : '已掉线' }}</el-tag>
          </el-checkbox>
        </el-checkbox-group>
        <div class="actions mt-2">
          <el-button type="warning" plain :loading="drillRunning" @click="runDrill('knock_out', 'down')">
            模拟掉线
          </el-button>
          <el-button type="danger" plain :loading="drillRunning" @click="runDrill('knock_out', 'destroyed')">
            模拟永久损毁
          </el-button>
          <el-button type="primary" plain :loading="drillRunning" @click="runDrill('restore')">
            恢复全部
          </el-button>
        </div>

        <el-alert
          v-if="drill && drill.impact"
          class="mt-3"
          :closable="false"
          :type="drill.impact.lost_count ? 'error' : (drill.impact.degraded_blocks ? 'warning' : 'success')"
          :title="drill.impact.lost_count
            ? `影响评估：${drill.impact.lost_count} 个分块将永久丢失（其全部副本均位于故障节点）`
            : (drill.impact.degraded_blocks
              ? `${drill.impact.degraded_blocks} 个分块的主副本不可用（其余副本仍在线，当前不影响读取）`
              : '当前无故障节点')"
          :description="drill.impact.note"
        />
        <div v-if="drill && drill.impact && drill.impact.files_affected && drill.impact.files_affected.length" class="mt-2">
          <span class="text-3" style="font-size: 13px">受影响文件：</span>
          <el-tag
            v-for="f in drill.impact.files_affected"
            :key="`${f.owner}/${f.file_key}`"
            size="small"
            type="danger"
            effect="plain"
            style="margin-right: 6px"
          >{{ f.owner }}/{{ f.file_key }}（将丢失 {{ f.lost_blocks }} 个分块）</el-tag>
        </div>
        <details v-if="drill && drill.faulty && drill.faulty.length" class="note-collapse">
          <summary>说明</summary>
          <p class="note">
            故障生效期间，对「完整性验证」或「文件详情」页中相关数据的读取会失败，
            对存放在故障节点上的副本的写入同样会失败。点击「恢复全部」后重新执行一次写入即可恢复一致，
            也可直接在「上传文件」页重新上传。
          </p>
        </details>

        <details class="note-collapse">
          <summary>说明</summary>
          <p class="note">
            勾选目标节点后点击「模拟掉线」或「模拟永久损毁」使其不可达，点击「恢复全部」即可撤销。
            掉线仅使该节点的读写失败，不删除数据；永久损毁会列出无法恢复的数据范围。
          </p>
        </details>
      </div>

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
              <span v-if="row.db" class="mono text-2" style="font-size: 13px">
                {{ row.db.blobs ?? '' }} 块 / {{ row.db.state ?? '' }} 状态
              </span>
              <span v-else class="text-3">—</span>
            </template>
          </el-table-column>
          <template #empty>
            <EmptyState title="暂无节点" />
          </template>
        </el-table>
      </div>

      <div class="panel mb-3">
        <h4 class="sec-title">块分布矩阵（行 = 存储槽位，列 = 节点）</h4>
        <BlockMatrix :nodes="nodes" />
      </div>

      <div class="panel mb-3">
        <h4 class="sec-title">存储证明（PoR）挑战</h4>
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
            <span>已挑战 {{ porResult.asked_total }} 个分块 / 已响应 {{ porResult.answered_total }} 个</span>
            <span>证据 {{ porResult.proof_size_bytes }} 字节</span>
            <span>合并份额 {{ porResult.aggregated_shares }}</span>
          </div>
          <el-table :data="porResult.shares || []" size="small" class="mt-2">
            <el-table-column prop="node_id" label="节点" width="100" />
            <el-table-column label="已挑战 / 已响应" width="140" align="center">
              <template #default="{ row }">
                <span class="mono">{{ row.asked }} / {{ row.answered }}</span>
              </template>
            </el-table-column>
            <el-table-column label="状态">
              <template #default="{ row }">
                <span v-if="row.unreachable" class="text-danger">不可达</span>
                <span v-else-if="row.error" class="text-danger">{{ row.error }}</span>
                <span v-else class="text-ok">已答</span>
              </template>
            </el-table-column>
          </el-table>
          <StageTimeline v-if="porResult.timings" :timings="porResult.timings" :total-ms="porElapsed" class="mt-3" />
        </div>

        <details class="note-collapse">
          <summary>说明</summary>
          <p class="note">
            设定挑战抽取的位置数后点击「发起挑战」，核验各节点是否仍持有其名下的分块密文。
            该操作只读取密文并重算分量，不修改任何数据。
          </p>
        </details>
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
  font-size: 17px;
  font-weight: 600;
  color: var(--text-1);
  margin-bottom: 14px;
}
.note {
  font-size: 13px;
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
/* ★ 故障演练面板 */
.actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}
/* 勾选节点那一排：窄屏下会自动折行，别挤成一团 */
.drill-pick {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
  line-height: 2;
}
</style>
