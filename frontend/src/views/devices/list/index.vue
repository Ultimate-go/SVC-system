<script setup>
/**
 * 设备（存储节点）列表。
 *
 * - StatCard 行：节点总数 / 在线数 / 离线列表 / 副本数 / 副本不足块数。
 * - 节点表格：node_id / n / held / span / 一致性(valid) / 本地自检(nodeSelfCheck) / db。
 * - 块分布矩阵（BlockMatrix）：行=存储槽位，列=节点。
 * - PoR 挑战卡：lambda_pos 可调 → POST /api/por，逐台 asked:answered。
 * - 待补推横幅 + 补推按钮（仅管理员）。
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

const retrying = ref(false)

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
      else ElMessage.warning('补推完成，但还有几台没通；起来后再点一次')
    await load()
  } catch {
    // 错误已由拦截器弹出
  } finally {
    retrying.value = false
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
      ElMessage.warning('先勾选要出事的节点')
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
      ElMessage.success('已撤销模拟：所有节点恢复在线')
    } else {
      ElMessage.warning(
        mode === 'destroyed'
          ? '已模拟永久损毁（不可逆）；看下面的影响面，再去验证 / 改块页试试'
          : '已模拟掉线（可恢复）；看下面的影响面，再去验证 / 改块页试试',
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
    <PageHeader title="设备（存储节点）" subtitle="每台服务器一个进程，各自一份 SQLite">
      <el-button @click="refreshAll"><Icon name="refresh" :size="14" style="margin-right: 6px" />刷新</el-button>
    </PageHeader>

    <!-- ★ 仅管理员：改集群规模。放在最上面 —— 这个页面在讲的就是"几台机器"。 -->
    <NodeCountCard v-if="isAdmin" ref="deployCard" />

    <!-- ★ 紧跟台数卡："几台"与"各自在哪个端口"是同一件事的两面，
         改台数之后端口那几行也要跟着变（两张卡片靠 deployBus 通信）。 -->
    <PortsCard v-if="isAdmin" ref="portsCard" />

    <el-alert
      v-if="hasPending"
      type="warning"
      :closable="false"
      class="mb-3"
      :title="pending?.restored ? '上次写失败的现场已经跨重启保住了' : '有写推未完成'"
    >
      <template #default>
        <p class="mb-2">
          别重新上传（那批下标已经分配过）。把机器弄活，点「补推」即可收敛
          —— 也可以不动手：后台会自己试几轮。
        </p>
        <!-- ★ 后端在启动时把这份现场从盘上装回来，会留一句“它是什么、该怎么办”。
             把原话摆出来（而不是界面上重新编一句）—— 真相只有一份。 -->
        <p v-if="pending?.site_note" class="mb-2">{{ pending.site_note }}</p>
        <el-button v-if="isAdmin" size="small" :loading="retrying" @click="retryPush">补推</el-button>
      </template>
    </el-alert>

    <div v-if="error" class="panel"><p class="text-danger">{{ error }}</p></div>
    <template v-else>
      <!-- ★ 仅管理员：故障演练。放在节点现状之前 —— 它就是“让这些机器出事”。 -->
      <div v-if="isAdmin" class="panel mb-3">
        <h4 class="sec-title">故障演练（模拟节点突然下线 / 永久损毁）</h4>
        <p class="note">
          只做一件事：让指定节点「联系不上」—— <b>读</b>当场失败，<b>写</b>逐台失败。
          <b>密码学与分片一个字都不改</b> —— 所以接下来看到的失败，
          是真代码在「这台机器没了」时的真实行为。
          <br />
          <b>它不会删任何数据</b>：只把节点标成「不可达」，
          点「恢复全部」立刻全部复原。
          <br />
          <b>掉线</b>：机器联系不上（网线拔了 / 进程挂了）。
          &nbsp;<b>永久损毁</b>：现实中磁盘也没了（地震 / 海啸）——
          影响面会告诉你「如果这是真的，哪几块就永远回不来了」。
        </p>
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
            ? `如果这真的是地震：${drill.impact.lost_count} 块会永久丢失`
            : (drill.impact.degraded_blocks
              ? `${drill.impact.degraded_blocks} 块的主副本出事（副本仍在，暂时照常可用）`
              : '当前无故障')"
          :description="drill.impact.note"
        />
        <div v-if="drill && drill.impact && drill.impact.files_affected && drill.impact.files_affected.length" class="mt-2">
          <span class="text-3" style="font-size: 12px">受影响的文件：</span>
          <el-tag
            v-for="f in drill.impact.files_affected"
            :key="`${f.owner}/${f.file_key}`"
            size="small"
            type="danger"
            effect="plain"
            style="margin-right: 6px"
          >{{ f.owner }}/{{ f.file_key }}（会丢 {{ f.lost_blocks }} 块）</el-tag>
        </div>
        <p v-if="drill && drill.faulty && drill.faulty.length" class="note mt-2">
          现在可以去试：<b>完整性验证</b> / <b>文件详情</b> 取块 —— 只要涉及的块在故障机器上，
          就会看到取不到密文的报错；<b>改块 / 追加 / 截断</b> 会失败并留下「待补推」现场
          （写推不到那几台）—— 点「恢复全部」之后，到「文件与块」页点一次「补推」即可收敛。
          <br />
          <b>这份现场过得了重启</b>：就算这时候把服务停掉再起（写失败的机器还没弄活），
          重启后现场还在、这个横幅也还在，不会因为一次重启就要求人工去节点上清数据。
        </p>
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
        <h4 class="sec-title">块分布矩阵（行 = 存储槽位，列 = 节点）</h4>
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
