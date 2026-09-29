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
const usageRatio = computed(() => {
  const max = Number(crs.value.n_max || 0)
  return max > 0 ? Math.round((Number(delta.value.n || 0) / max) * 100) : 0
})

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
    ElMessage.success('自检通过')
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
      title="有写推未完成 —— 全网摘要暂时不一致"
      description="不要重新上传（登记表已分配过那批下标，重传会拿到新下标）。请到「设备」页点「补推」。"
    >
      <template #default>
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
      <StatCard label="块哈希" :value="crs.l" unit="位" hint="每段密文的比特数" />
      <StatCard label="素数位长" :value="crs.prime_bits" unit="位" hint="块哈希+1" />
      <StatCard label="位置上限" :value="crs.n_max" hint="不可更改" />
    </div>

    <div class="section-heading section-heading-spaced">
      <div><span class="section-kicker mono">STORAGE FOOTPRINT</span><h2>存储规模</h2></div>
      <span class="section-note mono">LIVE COUNTERS</span>
    </div>
    <div class="grid">
      <StatCard label="文件数" :value="status.files" hint="全系统所有文件" />
      <StatCard label="块数" :value="status.blocks" hint="全局向量已用位置" />
      <StatCard label="副本数" :value="status.replica_factor" hint="每块默认存几份" />
      <StatCard
        label="副本不足"
        :value="status.under_replicated"
        :hint="status.under_replicated ? '开副本之前传的老数据，重传可补齐' : '当前无缺口'"
      />
    </div>

    <div class="compact-sections">
      <section class="compact-section">
        <div class="section-heading">
          <div><span class="section-kicker mono">GLOBAL COMMITMENT</span><h2>全局摘要 <span class="mono">δ = (U, C, n)</span></h2></div>
          <span class="section-note mono">{{ usageRatio }}% USED</span>
        </div>
        <div class="panel commitment-panel">
          <div class="panel-title-row"><span class="panel-subtitle">全局向量的公开指纹与已用位置</span><span class="live-mark"><i />SYNCED</span></div>
          <div class="delta-row">
            <RingGauge :value="delta.n" :max="crs.n_max" label="已用位置" />
            <div class="delta-detail">
              <div class="delta-item"><span class="k">n</span><span class="v mono">{{ delta.n }}</span><span class="hint">全局块数</span></div>
              <div class="delta-item"><span class="k">U</span><span class="v mono" :title="delta.U">{{ hexFp(delta.U, 16) }}</span><span class="hint">摘要指纹 · 前 16 位</span></div>
              <div class="delta-item"><span class="k">C</span><span class="v mono" :title="delta.C">{{ hexFp(delta.C, 16) }}</span><span class="hint">摘要指纹 · 前 16 位</span></div>
            </div>
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
          <div class="panel-title-row"><span class="panel-subtitle">私钥仅在登录会话中解封</span><span class="live-mark"><i />PROTECTED</span></div>
          <div class="keywrap">
            <div class="keywrap-item"><span class="k">方案</span><span class="v">{{ keywrap.scheme }}</span></div>
            <div class="keywrap-item"><span class="k">用户 / 有密钥</span><span class="v mono">{{ keywrap.users }} / {{ keywrap.user_keys }}</span></div>
            <div class="keywrap-item"><span class="k">会话私钥在内存</span><span class="v mono">{{ keywrap.sessions_with_key }}</span></div>
            <div class="keywrap-item"><span class="k">私钥存储位置</span><span class="v">{{ keywrap.private_key_storage }}</span></div>
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
