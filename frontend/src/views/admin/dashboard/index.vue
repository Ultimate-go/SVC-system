<script setup>
/**
 * 总览页（★ 2026-10-12 起对全体用户开放：普通用户与管理员都进这一页）。
 * 展示：公开参数、全局摘要 δ、规模、集群健康、密钥模型、待补推横幅、自检。
 *
 * ★ 自检按钮也对所有人可用（后端 ``POST /api/admin/check`` 已改为
 *   ``current_user``）：它只读，失败原因会原样显示。
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { systemApi } from '../../../api/system'
import { devicesApi } from '../../../api/devices'
import { fmtAgo, hexFp } from '../../../utils/format'
import StatCard from '../../../components/common/StatCard.vue'
import RingGauge from '../../../components/chart/RingGauge.vue'
import ProcessingFlow from '../../../components/chart/ProcessingFlow.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'
import Icon from '../../../components/icons/Icon.vue'

const router = useRouter()

const loading = ref(true)
const error = ref('')
const status = ref(null)
const pending = ref(null)
const checking = ref(false)
const checkResult = ref(null)

// ---- 系统自检：逐项推进（与后端 store.check() 的检查逻辑一一对应） ----
const CHECK_ITEMS = [
  { key: 'registry', name: '位置段登记表', desc: '登记表自身一致' },
  { key: 'sync', name: '登记表与向量', desc: '登记表与向量同步' },
  { key: 'reach', name: '节点可达性', desc: '所有节点可联系' },
  { key: 'views', name: '节点本地视图', desc: '每台节点视图合法' },
  { key: 'held', name: '节点持有核对', desc: '声称持有与实际存着一致' },
  { key: 'n_sync', name: '块数同步', desc: '所有节点停在同一个块数' },
  { key: 'holders', name: '持有者集合', desc: '实际持有者与在用位置一致' },
  { key: 'replica_list', name: '副本账实核对', desc: '每个下标副本列表与账目一致' },
  { key: 'replica_cover', name: '副本覆盖', desc: '每个下标至少一份副本' },
  { key: 'digest', name: '增量摘要一致性', desc: '增量摘要与一次性承诺一致' },
]

// 自检失败时，把后端报错按关键词定位到具体检查项（best-effort）。
const FAIL_LOCATE = [
  { key: 'registry', re: /登记表/ },
  { key: 'n_sync', re: /还没收敛|停在 n=|没跟上/ },
  { key: 'reach', re: /联系不上|不可达/ },
  { key: 'held', re: /声称|密文不一致/ },
  { key: 'views', re: /视图不合法/ },
  { key: 'holders', re: /持有者|账实不符/ },
  { key: 'replica_cover', re: /一份副本都没有/ },
  { key: 'replica_list', re: /记的副本是|多出来|少了/ },
  { key: 'digest', re: /摘要|承诺/ },
]

const checkSteps = ref([])
const passedCount = computed(() => checkSteps.value.filter((s) => s.state === 'pass').length)

/* ---------------------------------------------------------------------------
   上次自检的结论（存在**本机浏览器**里）
   ---------------------------------------------------------------------------
   后端没有“自检历史”接口，审计流水又只有管理员看得到 —— 普通用户想知道
   “上一次查是什么时候、结果如何”，只能靠本机记一笔。所以文案就写
   “本机上次自检”，不假装它是服务端记录。清理浏览器数据就会没，不影响自检本身。
--------------------------------------------------------------------------- */
const LAST_CHECK_KEY = 'vds_last_check_v1'

function readLastCheck() {
  try {
    const o = JSON.parse(localStorage.getItem(LAST_CHECK_KEY) || 'null')
    return o && typeof o.at === 'number' ? o : null
  } catch {
    return null
  }
}

const lastCheck = ref(readLastCheck())

function saveLastCheck(record) {
  lastCheck.value = record
  try {
    localStorage.setItem(LAST_CHECK_KEY, JSON.stringify(record))
  } catch {
    /* 隐私模式写不进去：不影响自检结论本身 */
  }
}

/* 逐文件摘要默认只铺开头几条：文件多了这一块会没完没了地长。 */
const FILE_PREVIEW = 4
const showAllFiles = ref(false)
const deltaFiles = computed(() => delta.value.files || [])
const visibleFiles = computed(() =>
  showAllFiles.value ? deltaFiles.value : deltaFiles.value.slice(0, FILE_PREVIEW),
)
const hiddenFileCount = computed(() => Math.max(0, deltaFiles.value.length - FILE_PREVIEW))

// 逐项扫描节奏：每项约 1 秒，10 项合计约 10.5 秒，让过程从容、不显仓促。
const SCAN_MS = 950
const STEP_GAP_MS = 100

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function locateFailKey(message) {
  for (const it of FAIL_LOCATE) if (it.re.test(message)) return it.key
  return null
}

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
  if (checking.value) return
  checking.value = true
  checkResult.value = null
  checkSteps.value = CHECK_ITEMS.map((it) => ({ ...it, state: 'pending' }))

  // 真实自检在后台整体执行（一次请求、六项一起查），动画过程逐项揭示。
  const real = systemApi
    .check()
    .then((d) => ({ ok: true, data: d.data }))
    .catch((e) => ({ ok: false, message: e?.response?.data?.detail || e?.message || '自检失败' }))

  // 逐项扫描：先亮起扫描态，再落为通过。
  for (let i = 0; i < CHECK_ITEMS.length; i++) {
    const step = checkSteps.value[i]
    step.state = 'running'
    await sleep(SCAN_MS)
    step.state = 'pass'
    await sleep(STEP_GAP_MS)
  }

  // 揭晓真实结论。
  const r = await real
  if (r.ok) {
    checkResult.value = r.data
    saveLastCheck({
      at: Date.now(),
      ok: true,
      passed: CHECK_ITEMS.length,
      total: CHECK_ITEMS.length,
      message: r.data?.message || '全部通过',
    })
  } else {
    const failKey = locateFailKey(r.message)
    const failIdx = failKey
      ? CHECK_ITEMS.findIndex((x) => x.key === failKey)
      : CHECK_ITEMS.length - 1
    checkSteps.value.forEach((s, i) => {
      if (i < failIdx) s.state = 'pass'
      else if (i === failIdx) s.state = 'fail'
      else s.state = 'pending'
    })
    checkResult.value = { ok: false, message: r.message }
    saveLastCheck({
      at: Date.now(),
      ok: false,
      passed: failIdx,
      total: CHECK_ITEMS.length,
      message: r.message,
    })
  }
  checking.value = false
}

onMounted(() => {
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
      <!-- 管线下面是**当前真实规模**（全部取自 /api/system/status，没有编造值）：
           管线上游画的是流程，这里补一句“这套流程现在跑了多少东西”。 -->
      <div class="hero-scale">
        <span class="hero-scale-item"><b class="mono">{{ status.files || 0 }}</b>份文件</span>
        <span class="hero-scale-item"><b class="mono">{{ status.blocks || 0 }}</b>个块</span>
        <span class="hero-scale-item"><b class="mono">{{ status.nodes || 0 }}</b>台节点</span>
        <span class="hero-scale-item"><b class="mono">{{ status.replica_factor || 0 }}</b>份副本</span>
        <span class="hero-scale-hint mono">LIVE · {{ status.mode }} · {{ status.transport }}</span>
      </div>
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
              <div class="delta-item"><span class="k">向量</span><span class="v mono">{{ deltaFiles.length }}</span><span class="hint">每份文件一条</span></div>
              <div class="delta-item">
                <span class="k">δ 指纹</span>
                <span
                  class="v mono"
                  :title="deltaFiles.length === 1 ? ('U = ' + deltaFiles[0].U + '\nC = ' + deltaFiles[0].C) : ''"
                >{{ deltaFiles.length === 1 ? hexFp(deltaFiles[0].delta_fp, 16) : '每份各不相同' }}</span>
                <span class="hint">一份文件一个值</span>
              </div>
            </div>
          </div>
          <div
            v-for="f in visibleFiles"
            :key="`${f.owner}/${f.file_key}`"
            class="panel-subtitle mono"
            style="white-space: normal; margin-top: 6px"
            :title="'U = ' + f.U + '\nC = ' + f.C"
          >
            {{ f.owner }} / {{ f.file_key }} · n={{ f.n }} · offset={{ f.offset }} ·
            {{ hexFp(f.delta_fp, 16) }}
          </div>
          <button v-if="hiddenFileCount" type="button" class="more-toggle" @click="showAllFiles = !showAllFiles">
            {{ showAllFiles ? '收起' : `展开其余 ${hiddenFileCount} 条` }}
          </button>
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

      <section class="compact-section compact-section--full">
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

      <section class="compact-section compact-section--full">
        <div class="section-heading">
          <div><span class="section-kicker mono">SYSTEM DIAGNOSTICS</span><h2>系统自检</h2></div>
          <span class="section-note mono">ON DEMAND</span>
        </div>
        <div class="panel diagnostics-panel">
          <div class="panel-title-row">
            <span class="panel-subtitle">逐项核对集群与向量一致性</span>
            <span class="live-mark" :class="{ 'is-scan': checking }"><i />{{ checking ? 'SCANNING' : 'ON DEMAND' }}</span>
          </div>

          <div class="diag-actions">
            <el-button :loading="checking" @click="runCheck"><Icon name="refresh" :size="14" style="margin-right: 6px" />启动自检</el-button>
            <p v-if="lastCheck" class="last-check mono">
              本机上次自检 · {{ fmtAgo(lastCheck.at) }} ·
              <span :class="lastCheck.ok ? 'text-ok' : 'text-danger'">{{ lastCheck.ok
                ? `全部通过 ${lastCheck.passed}/${lastCheck.total}`
                : `未通过（${lastCheck.passed}/${lastCheck.total}）：${lastCheck.message}` }}</span>
            </p>
          </div>

          <!-- 逐项扫描：状态标识 + 扫描动效 -->
          <div v-if="checkSteps.length" class="check-list">
            <div class="check-progress">
              <span class="mono check-progress-label">SCANNING {{ passedCount }} / {{ checkSteps.length }}</span>
              <div class="check-progress-bar"><span :style="{ width: (passedCount / checkSteps.length) * 100 + '%' }" /></div>
            </div>

            <div
              v-for="s in checkSteps"
              :key="s.key"
              class="check-item"
              :class="'is-' + s.state"
            >
              <span class="check-state">
                <span v-if="s.state === 'running'" class="spinner" />
                <span v-else-if="s.state === 'pass'" class="mark mark-pass">✓</span>
                <span v-else-if="s.state === 'fail'" class="mark mark-fail">✕</span>
                <span v-else class="dot" />
              </span>
              <span class="check-name">{{ s.name }}</span>
              <span class="check-desc">{{ s.desc }}</span>
              <span v-if="s.state === 'running'" class="scanline" aria-hidden="true" />
            </div>
          </div>

          <!-- 最终结论：全部检查完成后才出现 -->
          <div v-if="checkResult" class="check-verdict">
            <el-alert :type="checkResult.ok ? 'success' : 'error'" :closable="false" :title="checkResult.message" />
            <el-alert
              v-for="(note, i) in checkResult.notes || []"
              :key="i"
              type="info"
              :closable="false"
              class="mt-2"
              title="提示"
              :description="note"
            />
            <StageTimeline
              v-if="checkResult.timings"
              :timings="checkResult.timings"
              :state="checkResult.ok ? 'ok' : 'fail'"
              class="mt-2"
            />
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
/* 管线下面那行“当前规模”：数字取自 /api/system/status，实时刷新。 */
.hero-scale { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 26px; margin-top: 18px; padding-top: 15px; border-top: 1px solid var(--line); position: relative; z-index: 1; }
.hero-scale-item { color: var(--text-2); font-size: 12px; }
.hero-scale-item b { color: var(--text-1); font-size: 17px; font-weight: 500; margin-right: 4px; }
.hero-scale-hint { margin-left: auto; color: var(--text-3); font-size: 9px; letter-spacing: .1em; }
/* 逐文件摘要的展开/收起 */
.more-toggle { align-self: flex-start; margin-top: 10px; padding: 3px 0; color: var(--accent); font: inherit; font-size: 12px; background: none; border: 0; cursor: pointer; }
.more-toggle:hover { text-decoration: underline; }
/* 自检按钮与“上次自检”一行 */
.diag-actions { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.last-check { margin: 0; color: var(--text-3); font-size: 11px; }
.section-heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; margin: 18px 1px 10px; }
.section-heading-spaced { margin-top: 27px; }
.section-heading h2 { color: var(--text-1); font-size: 16px; font-weight: 600; margin: 5px 0 0; }
.section-note { color: var(--text-3); font-size: 10px; letter-spacing: .08em; }
.compact-sections { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px 16px; margin-top: 6px; }
.compact-section { min-width: 0; display: flex; flex-direction: column; }
/* 自检 / 密钥模型整行显示：向下延伸时不再撑高同行的相邻卡片 */
.compact-section--full { grid-column: 1 / -1; }
.compact-section .section-heading { margin-top: 0; min-height: 43px; }
.compact-section > .panel { flex: 1 1 auto; min-height: 214px; box-sizing: border-box; }
/* 逐文件摘要会随文件数变长（默认只铺 4 条、可展开），所以它只保底高度、
   不设死高度；右边的运行状态卡则是恒定的几行，保持定高两边才对齐。 */
.commitment-panel { height: auto; min-height: 214px; }
.health-panel { height: 214px; }
/* 整行的面板自适应高度，向下延伸不再受固定高度限制 */
.compact-section--full > .panel { min-height: 0; height: auto; }
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
.keywrap {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 28px;
}
.keywrap-item {
  display: flex;
  gap: 8px;
  align-items: baseline;
  padding: 6px 0;
  font-size: 13px;
}
.keywrap-item .k {
  color: var(--text-3);
  white-space: nowrap;
}
.keywrap-item .v {
  color: var(--text-1);
}
.keywrap-note {
  font-size: 13px;
  color: var(--text-2);
  margin-top: 8px;
}

/* ---- 系统自检：逐项扫描 ---- */
.check-list {
  margin-top: 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.check-progress {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.check-progress-label {
  font-size: 10px;
  letter-spacing: .12em;
  color: var(--accent);
  white-space: nowrap;
}
.check-progress-bar {
  flex: 1;
  height: 3px;
  border-radius: 2px;
  background: var(--bg-raised);
  overflow: hidden;
}
.check-progress-bar span {
  display: block;
  height: 100%;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
  border-radius: 2px;
  transition: width .3s ease;
}
.check-item {
  position: relative;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid transparent;
  border-radius: 8px;
  overflow: hidden;
  background: var(--bg-raised);
  transition: border-color .2s ease, background .2s ease, opacity .2s ease;
}
.check-item.is-running {
  border-color: color-mix(in srgb, var(--accent) 35%, transparent);
  background: color-mix(in srgb, var(--accent) 6%, var(--bg-raised));
}
.check-item.is-pass .check-name { color: var(--text-1); }
.check-item.is-fail {
  border-color: color-mix(in srgb, var(--danger) 40%, transparent);
  background: color-mix(in srgb, var(--danger) 8%, var(--bg-raised));
}
.check-item.is-pending { opacity: .45; }
.check-state {
  width: 16px;
  height: 16px;
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
}
.spinner {
  width: 13px;
  height: 13px;
  border: 2px solid color-mix(in srgb, var(--accent) 25%, transparent);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin .7s linear infinite;
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--text-3);
}
.mark {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  line-height: 1;
  animation: pop .22s ease;
}
.mark-pass {
  color: var(--ok);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 14%, transparent);
}
.mark-fail {
  color: var(--danger);
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger) 16%, transparent);
}
.check-name {
  font-size: 13px;
  color: var(--text-2);
  white-space: nowrap;
}
.check-desc {
  margin-left: auto;
  font-size: 11px;
  color: var(--text-3);
  text-align: right;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.scanline {
  position: absolute;
  top: 0;
  bottom: 0;
  left: 0;
  width: 45%;
  background: linear-gradient(90deg, transparent, color-mix(in srgb, var(--accent) 16%, transparent), transparent);
  animation: scan 1s linear infinite;
  pointer-events: none;
}
.check-verdict {
  margin-top: 14px;
  animation: fadein .3s ease;
}
.live-mark.is-scan i {
  background: var(--accent);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 15%, transparent);
  animation: pulse 1s ease-in-out infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes scan { 0% { transform: translateX(-110%); } 100% { transform: translateX(330%); } }
@keyframes pop { 0% { transform: scale(.5); } 100% { transform: scale(1); } }
@keyframes fadein { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }

@media (max-width: 900px) {
  .compact-sections { grid-template-columns: 1fr; }
  .compact-section > .panel, .commitment-panel, .health-panel, .key-panel, .diagnostics-panel { height: auto; min-height: 0; }
}
/* 大屏（≥ 1500px）：卡片不被拉得过宽，一行别拉出一百多字的长句。 */
@media (min-width: 1500px) {
  .dashboard { max-width: 1560px; margin-left: auto; margin-right: auto; }
  .grid { grid-template-columns: repeat(auto-fill, minmax(216px, 1fr)); }
  .compact-sections { gap: 22px 20px; }
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
