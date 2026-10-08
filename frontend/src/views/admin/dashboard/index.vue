<script setup>
/**
 * 总览页。普通用户访问 / 时重定向到 /files。
 * 管理员看到：公开参数、全局摘要 δ、规模、集群健康、密钥模型、待补推横幅、自检。
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { usePermission } from '../../../composables/usePermission'
import { systemApi } from '../../../api/system'
import { devicesApi } from '../../../api/devices'
import { hexFp } from '../../../utils/format'
import StatCard from '../../../components/common/StatCard.vue'
import RingGauge from '../../../components/chart/RingGauge.vue'
import ProcessingFlow from '../../../components/chart/ProcessingFlow.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'
import Icon from '../../../components/icons/Icon.vue'

const { isAdmin } = usePermission()
const router = useRouter()

const loading = ref(true)
const error = ref('')
const status = ref(null)
const pending = ref(null)
const checking = ref(false)
const checkResult = ref(null)

const delta = computed(() => status.value?.delta || {})
const crs = computed(() => status.value?.crs || {})
const keywrap = computed(() => status.value?.keywrap || {})
const clusterHealthy = computed(() => !status.value?.nodes_down?.length && !status.value?.under_replicated)

/**
 * 副本达标的块数 —— 给那个环形仪表用。
 *
 * ★ 以前这个仪表画的是「已用位置 / n_max」。改造后**位置上限不存在了**
 *   （素数按块身份派生、槽位可以无限增长），"用了百分之多少"这个数
 *   失去了意义 —— 分母是一个与业务无关的常数。
 *   换成「多少块副本是齐的」：同样的位置、同样一眼看得懂，
 *   而且它**真的**在描述集群健康度（核副本不再是装饰）。
 */
const replicaOk = computed(() =>
  Math.max(0, Number(status.value?.blocks || 0) - Number(status.value?.under_replicated || 0)),
)

const hasPending = computed(() => {
  const p = pending.value
  return !!(p?.pending || p?.persist_pending)
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [s, p] = await Promise.all([systemApi.status(), devicesApi.pending()])
    status.value = s.data
    pending.value = p.data
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function runCheck() {
  checking.value = true
  checkResult.value = null
  try {
    const { data } = await systemApi.check()
    checkResult.value = data
    // ★ 自检可能带 notes：那是**提示**（例如“被摘掉的机器在重启前还留着旧副本”），
    //   不是失败。用 warning 说一句，别把它混进那句绿色的“自检通过”里。
    if (data.notes?.length) ElMessage.warning('自检通过（附一条提示）')
    else ElMessage.success('自检通过')
  } catch (e) {
    checkResult.value = { ok: false, message: e?.response?.data?.detail || '自检失败' }
  } finally {
    checking.value = false
  }
}

onMounted(() => {
  if (!isAdmin.value) {
    router.replace('/files')
    return
  }
  load()
})
</script>

<template>
  <div v-if="loading" class="loading" v-loading="true" />
  <div v-else-if="error" class="panel">
    <p class="text-danger">{{ error }}</p>
    <el-button class="mt-3" @click="load">重试</el-button>
  </div>
  <div v-else-if="status" class="dashboard">
    <el-alert
      v-if="hasPending"
      type="warning"
      :closable="false"
      class="mb-3"
      :title="pending?.restored ? '写失败状态已跨重启持久化' : '写操作未完成，摘要暂不一致'"
    >
      <template #default>
        <p class="mb-2">
          请勿重复上传（该批全局下标已分配）。可在「设备」页执行补推；系统亦以固定间隔自动重试。
        </p>
        <p v-if="pending?.site_note" class="mb-2">{{ pending.site_note }}</p>
        <el-button size="small" @click="router.push('/devices')">去补推</el-button>
      </template>
    </el-alert>

    <section class="hero-panel panel mb-3" aria-labelledby="overview-title">
      <div class="overview-head">
        <div>
          <span class="eyebrow mono">SYSTEM OVERVIEW / IAVC-VDSS</span>
          <h1 id="overview-title">全局运行态势</h1>
          <p>从文件进入，到形成可验证证据的实时数据链路。</p>
        </div>
        <div class="health-pill" :class="clusterHealthy ? 'is-ok' : 'is-warn'">
          <span class="health-dot" />
          <span>{{ clusterHealthy ? '系统运行正常' : '需要关注' }}</span>
          <small class="mono">{{ status.nodes || 0 }} NODES</small>
        </div>
      </div>
      <ProcessingFlow />
    </section>

    <div class="section-heading">
      <div><span class="section-kicker mono">PUBLIC PARAMETERS</span><h2>系统参数</h2></div>
      <span class="section-note mono">CRS / STATIC</span>
    </div>
    <div class="grid">
      <StatCard label="公开参数" :value="crs.N_bits" unit="位" hint="隐藏阶群的模数位长" />
        <StatCard label="分量位长" :value="crs.l" unit="位" hint="一个分量的比特数" />
        <StatCard label="素数位长" :value="crs.prime_bits" unit="位" hint="l + 1" />
      <!-- ★ 以前这里是「位置上限 = n_max，不可更改」。改造后 n_max 不再是上限：
           素数按块身份派生，块数没有任何天花板。于是改成一个**真的**在涨的数：
           已分配过的存储槽位（含删除留下的空洞，所以它 ≥ 块数）。 -->
      <StatCard label="存储槽位（已分配）" :value="delta.slots_allocated" hint="含删除留下的空洞" />
    </div>

    <div class="section-heading section-heading-spaced">
      <div><span class="section-kicker mono">STORAGE FOOTPRINT</span><h2>存储规模</h2></div>
      <span class="section-note mono">LIVE COUNTERS</span>
    </div>
    <div class="grid">
      <StatCard label="文件数" :value="status.files" hint="全系统所有文件" />
      <StatCard label="块数" :value="status.blocks" hint="各文件块数之和" />
      <StatCard label="副本数" :value="status.replica_factor" hint="每块默认存几份" />
      <StatCard
        label="副本不足"
        :value="status.under_replicated"
        :hint="status.under_replicated ? '开副本前传的老数据，重传可补齐' : '当前无缺口'"
      />
    </div>

    <div class="compact-sections">
      <section class="compact-section">
        <div class="section-heading">
          <div><span class="section-kicker mono">COMMITMENTS</span><h2>逐文件摘要 <span class="mono">每份文件一条向量</span></h2></div>
          <span class="section-note mono">{{ status.replica_factor || 1 }}× REPLICA</span>
        </div>
        <div class="panel commitment-panel">
          <div class="panel-title-row"><span class="panel-subtitle">每份文件各自的公开指纹与已用位置</span><span class="live-mark"><i />SYNCED</span></div>
          <div class="delta-row">
            <RingGauge :value="replicaOk" :max="status.blocks || 1" label="副本达标" />
            <div class="delta-detail">
              <div class="delta-item"><span class="k">n</span><span class="v mono">{{ delta.n }}</span><span class="hint">在用块数（各文件之和）</span></div>
              <!-- ★ 架构变了：摘要是**逐文件**的，没有单一的全局 U / C。
                   以前这里显示 delta.U / delta.C —— 那两个字段在新架构里
                   根本不存在，于是永远显示 "—"。现在如实给条数 + 逐条列表。 -->
              <div class="delta-item"><span class="k">向量</span><span class="v mono">{{ (delta.files || []).length }}</span><span class="hint">每份文件一条</span></div>
              <div class="delta-item">
                <span class="k">δ 指纹</span>
                <span
                  class="v mono"
                  :title="(delta.files || []).length === 1 ? ('U = ' + delta.files[0].U + '\nC = ' + delta.files[0].C) : ''"
                >{{ (delta.files || []).length === 1 ? hexFp(delta.files[0].delta_fp, 16) : '每份各不相同' }}</span>
                <span class="hint">一份文件一个值</span>
              </div>
            </div>
          </div>
          <div
            v-for="f in (delta.files || [])"
            :key="`${f.owner}/${f.file_key}`"
            class="panel-subtitle mono"
            style="white-space: normal; margin-top: 6px"
            :title="'U = ' + f.U + '\nC = ' + f.C"
          >
            {{ f.owner }} / {{ f.file_key }} · n={{ f.n }} · offset={{ f.offset }} ·
            {{ hexFp(f.delta_fp, 16) }}
          </div>
        </div>
      </section>

      <section class="compact-section">
        <div class="section-heading">
          <div><span class="section-kicker mono">RUNTIME HEALTH</span><h2>运行状态</h2></div>
          <span class="section-note mono">CONTROL PLANE</span>
        </div>
        <div class="panel health-panel">
          <div class="panel-title-row"><span class="panel-subtitle">协调者与存储节点连接状态</span><span class="live-mark"><i />MONITORING</span></div>
          <div class="health-summary"><strong class="mono">{{ status.nodes || 0 }}</strong><span>个节点在线</span><span class="health-summary-status" :class="clusterHealthy ? 'text-ok' : 'text-warn'">{{ clusterHealthy ? '集群稳定' : '需要关注' }}</span></div>
          <div class="cluster-row">
            <span class="cluster-item">模式 <b class="mono">{{ status.mode }}</b></span>
            <span class="cluster-item">传输 <b class="mono">{{ status.transport }}</b></span>
            <span v-if="status.nodes_down?.length" class="cluster-item text-danger">离线：<b class="mono">{{ status.nodes_down.join(', ') }}</b></span>
            <span v-else class="cluster-item text-ok">全部在线</span>
          </div>
        </div>
      </section>

      <section class="compact-section">
        <div class="section-heading">
          <div><span class="section-kicker mono">KEY MANAGEMENT</span><h2>密钥模型</h2></div>
          <span class="section-note mono">NO MASTER KEY</span>
        </div>
        <div class="panel key-panel">
          <div class="panel-title-row"><span class="panel-subtitle">私钥在浏览器里解封，服务端不留</span><span class="live-mark"><i />PROTECTED</span></div>
          <div class="keywrap">
            <div class="keywrap-item"><span class="k">方案</span><span class="v">{{ keywrap.scheme }}</span></div>
            <div class="keywrap-item"><span class="k">用户 / 有密钥</span><span class="v mono">{{ keywrap.users }} / {{ keywrap.user_keys }}</span></div>
            <div class="keywrap-item"><span class="k">服务端内存中的私钥</span><span class="v mono">{{ keywrap.sessions_with_key }}<span class="text-3" style="font-size: 11px">（默认模型下恒为 0）</span></span></div>
            <div class="keywrap-item"><span class="k">私钥存储位置</span><span class="v">{{ keywrap.private_key_storage }}</span></div>
            <div class="keywrap-item"><span class="k">解封在哪发生</span><span class="v">{{ keywrap.session_key_location || '——' }}</span></div>
          </div>
        </div>
      </section>

      <section class="compact-section">
        <div class="section-heading">
          <div><span class="section-kicker mono">SYSTEM DIAGNOSTICS</span><h2>系统自检</h2></div>
          <span class="section-note mono">ON DEMAND</span>
        </div>
        <div class="panel diagnostics-panel">
          <div class="panel-title-row"><span class="panel-subtitle">检查摘要、节点状态与全局向量一致性</span></div>
          <el-button :loading="checking" @click="runCheck"><Icon name="refresh" :size="14" style="margin-right: 6px" />启动自检</el-button>
          <div v-if="checkResult" class="mt-3">
            <el-alert :type="checkResult.ok ? 'success' : 'error'" :closable="false" :title="checkResult.message" />
            <el-alert
              v-for="(note, i) in checkResult.notes || []"
              :key="i"
              type="info"
              :closable="false"
              class="mt-3"
              title="另外一条提示"
              :description="note"
            />
            <StageTimeline v-if="checkResult.timings" :timings="checkResult.timings" class="mt-3" />
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.loading {
  min-height: 200px;
}
.hero-panel {
  position: relative;
  overflow: hidden;
  background: radial-gradient(circle at 88% 8%, color-mix(in srgb, var(--accent) 9%, transparent), transparent 34%), var(--bg-panel);
}
.hero-panel::before {
  content: '';
  position: absolute;
  width: 220px;
  height: 220px;
  right: -100px;
  top: -120px;
  border: 1px solid color-mix(in srgb, var(--accent) 18%, transparent);
  border-radius: 50%;
  box-shadow: 0 0 0 24px color-mix(in srgb, var(--accent) 3%, transparent), 0 0 0 48px color-mix(in srgb, var(--accent) 2%, transparent);
  pointer-events: none;
}
.overview-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; margin-bottom: 24px; position: relative; z-index: 1; }
.eyebrow, .section-kicker { color: var(--accent); font-size: 10px; letter-spacing: .13em; }
.overview-head h1 { color: var(--text-1); font-size: 24px; letter-spacing: -.02em; margin: 7px 0 5px; }
.overview-head p { color: var(--text-2); font-size: 13px; margin: 0; }
.health-pill { display: inline-flex; align-items: center; gap: 8px; padding: 8px 11px; border: 1px solid var(--line); border-radius: 999px; color: var(--text-1); font-size: 12px; white-space: nowrap; background: color-mix(in srgb, var(--bg-raised) 78%, transparent); }
.health-pill small { color: var(--text-3); font-size: 9px; margin-left: 3px; }
.health-dot, .live-mark i { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 0 4px color-mix(in srgb, var(--ok) 12%, transparent); }
.health-pill.is-warn .health-dot { background: var(--warn); box-shadow: 0 0 0 4px color-mix(in srgb, var(--warn) 12%, transparent); }
.section-heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; margin: 18px 1px 10px; }
.section-heading-spaced { margin-top: 27px; }
.section-heading h2 { color: var(--text-1); font-size: 16px; font-weight: 600; margin: 5px 0 0; }
.section-note { color: var(--text-3); font-size: 10px; letter-spacing: .08em; }
.compact-sections { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px 16px; margin-top: 6px; }
.compact-section { min-width: 0; display: flex; flex-direction: column; }
.compact-section .section-heading { margin-top: 0; min-height: 43px; }
.compact-section > .panel { flex: 1 1 auto; min-height: 214px; box-sizing: border-box; }
.commitment-panel, .health-panel, .key-panel, .diagnostics-panel { height: 214px; }
.commitment-panel .delta-row { min-height: 112px; }
.health-summary { display: flex; align-items: baseline; gap: 8px; margin: 4px 0 17px; color: var(--text-2); font-size: 12px; }
.health-summary strong { color: var(--text-1); font-size: 31px; font-weight: 500; letter-spacing: -.04em; }
.health-summary-status { margin-left: auto; font-size: 11px; }
.panel-title-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 15px; }
.panel-subtitle { color: var(--text-2); font-size: 12px; }
.live-mark { display: inline-flex; align-items: center; gap: 7px; color: var(--text-3); font-size: 9px; letter-spacing: .08em; }
.live-mark i { display: inline-block; width: 5px; height: 5px; }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}
.sec-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 14px;
}
.delta-row {
  display: flex;
  gap: 28px;
  align-items: center;
}
.delta-detail {
  display: flex;
  flex-direction: column;
  gap: 10px;
  flex: 1;
}
.delta-item {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.delta-item .k {
  font-family: var(--font-mono);
  color: var(--accent);
  font-weight: 500;
  width: 20px;
}
.delta-item .v {
  color: var(--text-1);
  font-size: 15px;
}
.delta-item .hint {
  color: var(--text-3);
  font-size: 12px;
}
.cluster-row {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  font-size: 13px;
  color: var(--text-2);
}
.keywrap-item {
  display: flex;
  gap: 12px;
  padding: 6px 0;
  font-size: 13px;
}
.keywrap-item .k {
  color: var(--text-3);
  width: 140px;
  flex-shrink: 0;
}
.keywrap-item .v {
  color: var(--text-1);
}
.keywrap-note {
  font-size: 13px;
  color: var(--text-2);
  margin-top: 8px;
}
@media (max-width: 900px) {
  .compact-sections { grid-template-columns: 1fr; }
  .compact-section > .panel, .commitment-panel, .health-panel, .key-panel, .diagnostics-panel { height: auto; min-height: 0; }
}
@media (max-width: 560px) {
  .overview-head { flex-direction: column; }
  .health-pill { align-self: flex-start; }
  .delta-row { gap: 16px; }
  .delta-item { gap: 8px; }
  .delta-item .hint { display: none; }
  .delta-item .v { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  .section-heading { align-items: flex-start; }
  .section-note { padding-top: 4px; }
}
</style>
