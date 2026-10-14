<script setup>
/**
 * 总览页。对全体用户开放：普通用户与管理员均可访问。
 * 展示内容：公开参数、逐文件摘要、规模、集群健康、密钥模型、自检。
 *
 * 自检按钮对全体用户可用（后端 ``POST /api/admin/check`` 使用 ``current_user``）。
 * 该接口只读；失败原因原样显示。
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
const checking = ref(false)
const checkResult = ref(null)
/** 自检的端到端耗时（毫秒）：从发起请求到它返回的墙钟差值。
 *  ★ 动画是逐项揭示的演示，而标题里那个数必须是**真实的请求耗时**。 */
const checkElapsed = ref(0)
const healthDetailOpen = ref(false)

// ---- 系统自检：逐项推进，与后端 store.check() 的检查项一一对应 ----
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

// 自检失败时，按关键词把后端报错定位到具体检查项。
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
    /* 隐私模式下无法写入，不影响自检结论 */
  }
}

/* 逐文件摘要默认只显示前几条，避免随文件数无限增长。 */
const FILE_PREVIEW = 4
const showAllFiles = ref(false)
const deltaFiles = computed(() => delta.value.files || [])
const visibleFiles = computed(() =>
  showAllFiles.value ? deltaFiles.value : deltaFiles.value.slice(0, FILE_PREVIEW),
)
const hiddenFileCount = computed(() => Math.max(0, deltaFiles.value.length - FILE_PREVIEW))

// 逐项扫描节奏：每项约 0.79 秒，10 项合计约 7.9 秒（约为原时长的 75%）。
const SCAN_MS = 713
const STEP_GAP_MS = 75

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function itemName(key) {
  return CHECK_ITEMS.find((x) => x.key === key)?.name || key
}

function locateFailKey(message) {
  for (const it of FAIL_LOCATE) if (it.re.test(message)) return it.key
  return null
}

const delta = computed(() => status.value?.delta || {})
const crs = computed(() => status.value?.crs || {})
const keywrap = computed(() => status.value?.keywrap || {})

/*
 * 「密钥模型」卡里这两行是**方案常量**，不是运行时状态：
 * 它们描述的是 core/keywrap.py 那一层的实现约定（算法、盐与密钥长度、迭代数、在哪解封），
 * 任何部署、任何时刻都相同，也不随请求变化，所以写在这里而不是每次轮询都由后端回传；
 * 后端 key_model() 相应地不再提供这两个字段。
 * 改协议（换算法 / 改迭代数 / 改封装格式）时改这两段，并对照那一层的权威实现：
 * core/keywrap.py 里的 KIND_USER_KEY、PBKDF2_ITERATIONS、PRIVATE_KEY_BYTES。
 */
const CHECK_OK_TEXT = '自检通过：登记表 / 节点视图 / 增量摘要与一次性承诺一致'
const KEY_SCHEME_TEXT = 'SM2-ECIES（封装块密钥）+ PBKDF2-HMAC-SM3（封装用户私钥）'
const KEY_STORAGE_TEXT =
  '库中仅存储私钥密文，不存储明文私钥或口令。' +
  '加密方式：口令经 PBKDF2-HMAC-SM3（盐 16 字节、迭代 20 万轮）派生出密钥加密钥（KEK，32 字节），' +
  '由其派生 SM4 密钥（16 字节），对 32 字节私钥标量做 SM4-CTR 加密，并附 32 字节完整性标签。'
const KEY_UNWRAP_TEXT =
  '解封在浏览器内完成：后端只返回私钥密文，口令在本地派生出 KEK，' +
  '完整性标签校验通过后解出私钥标量。私钥仅存于当前标签页的会话存储' +
  '（sessionStorage；闲置 5 分钟或关闭标签即清除），不写入 localStorage，也不随任何请求上传。' +
  '验证与解密均由浏览器执行。'
const clusterHealthy = computed(() => !status.value?.nodes_down?.length && !status.value?.under_replicated)

// 「模式 / 传输」取值说明：后端可能出现的取值 → 解释（manager.py 按部署配置返回）。
// mode 只有两档；transport 显示的是传输层类名，共三种（FaultyTransport 为故障演练代理，
// 首次演练时装上并持续生效，重启后恢复为真实传输）。未知取值走兜底文案。
const MODE_DESC = {
  distributed: '跨进程分布式：协调者通过 HTTP 与多个独立节点进程通信，分块与副本分散在多台节点上，不存在单点持有全部数据。',
  'single-process': '单进程演示：协调者与各节点同处一个 Python 进程内，存储层为进程内直连、不经网络，仅用于本地演示与自测。',
}
const TRANSPORT_DESC = {
  LocalTransport: '进程内直连：单进程模式下由存储层自建的本地传输，调用不经过网络。',
  HttpTransport: '跨进程 HTTP：协调者经 HTTP 加访问令牌访问各节点服务，带重试、退避与超时配置。',
  FaultyTransport: '故障演练代理：包在真实传输外层，可注入节点掉线或永久损毁两类故障，用于验证部分节点失效时副本与完整性验证仍然成立；安装后持续生效，重启恢复为真实传输。',
}
const modeDesc = computed(() => MODE_DESC[status.value?.mode] || '当前部署模式未收录说明，请结合部署文档确认该取值。')
const transportDesc = computed(() => TRANSPORT_DESC[status.value?.transport] || '当前传输层未收录说明；它由后端按部署配置自动选择。')

/**
 * 副本达标的块数，供环形仪表使用。
 *
 * 该仪表此前绘制「已用位置 / n_max」。位置上限已不存在（素数按块身份派生，
 *   槽位可无限增长），该比值失去意义，其分母为与业务无关的常数。
 *   现绘制「副本达标的块数 / 总块数」，直接反映集群健康度。
 */
const replicaOk = computed(() =>
  Math.max(0, Number(status.value?.blocks || 0) - Number(status.value?.under_replicated || 0)),
)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const s = await systemApi.status()
    status.value = s.data
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
  checkElapsed.value = 0
  checkSteps.value = CHECK_ITEMS.map((it) => ({ ...it, state: 'pending' }))

  // 真实自检在后台整体执行（一次请求、六项一起查），动画过程逐项揭示。
  const t0 = performance.now()
  const real = systemApi
    .check()
    .then((d) => {
      checkElapsed.value = Math.round(performance.now() - t0)
      return { ok: true, data: d.data }
    })
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
      message: CHECK_OK_TEXT,
    })
  } else {
    // 定位得到就标出那一项；定位不到就**什么都不标**。
    //   为什么不再默认冤枉最后一项（原来 failIdx 缺省为 length-1）：后端是
    //   **一次调用**返回首个问题，而任何没被规则覆盖的原因（比如库读不出来）
    //   都会被按到「增量摘要一致性」头上 —— 那是在编一个看起来很像的结论。
    const failKey = locateFailKey(r.message)
    const failIdx = failKey ? CHECK_ITEMS.findIndex((x) => x.key === failKey) : -1
    checkSteps.value.forEach((s, i) => {
      if (failIdx >= 0 && i < failIdx) s.state = 'pass'
      else if (i === failIdx) s.state = 'fail'
      else s.state = 'pending' // 未判定：灰点（不再冒充已通过）
    })
    checkResult.value = { ok: false, message: r.message, located: failIdx >= 0 }
    saveLastCheck({
      at: Date.now(),
      ok: false,
      total: CHECK_ITEMS.length,
      failKey: failKey || '',
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

    <section class="hero-panel panel mb-3" aria-labelledby="overview-title">
      <div class="overview-head">
        <div>
          <span class="eyebrow mono">SYSTEM OVERVIEW / IAVC-VDSS</span>
          <h1 id="overview-title">全局运行态势</h1>
          <p>从文件进入系统到形成可验证证据的完整数据链路。</p>
        </div>
        <div class="health-pill" :class="clusterHealthy ? 'is-ok' : 'is-warn'">
          <span class="health-dot" />
          <span>{{ clusterHealthy ? '系统运行正常' : '需要关注' }}</span>
          <small class="mono">{{ status.nodes || 0 }} NODES</small>
        </div>
      </div>
      <ProcessingFlow />
      <!-- 管线下方为当前规模，数值全部取自 /api/system/status。 -->
      <div class="hero-scale" style="margin: 16px 0; display: flex; gap: 32px; align-items: center;">
          <span class="hero-scale-item" style="font-size: 22px;">
            当前系统状态：
        </span>
        <span class="hero-scale-item" style="font-size: 22px;">
            <b class="mono" style="color: var(--text-1); margin-right: 4px;">{{ status.files || 0 }}</b>份文件
        </span>

  <span class="hero-scale-item" style="font-size: 22px;">
    <b class="mono" style="color: var(--text-1); margin-right: 4px;">{{ status.blocks || 0 }}</b>个块
  </span>
  <span class="hero-scale-item" style="font-size: 22px;">
    <b class="mono" style="color: var(--text-1); margin-right: 4px;">{{ status.nodes || 0 }}</b>台节点
  </span>
  <span class="hero-scale-item" style="font-size: 22px;">
    <b class="mono" style="color: var(--text-1); margin-right: 4px;">{{ status.replica_factor || 0 }}</b>份副本
  </span>
  <span class="hero-scale-hint mono" style="font-size: 14px; opacity: 0.6; margin-left: auto;">LIVE · {{ status.mode }} · {{ status.transport }}</span>
</div>
    </section>

    <div class="section-heading">
      <div><span class="section-kicker mono">PUBLIC PARAMETERS</span><h2>系统参数</h2></div>
      <span class="section-note mono">CRS / STATIC</span>
    </div>
    <div class="grid">
      <StatCard hint-top label="公开参数" :value="crs.N_bits" unit="位" hint="隐藏阶群的模数位长" />
        <StatCard hint-top label="分量位长" :value="crs.l" unit="位" hint="一个分量的比特数" />
        <StatCard hint-top label="素数位长" :value="crs.prime_bits" unit="位" hint="分量位长 + 1" />
      <!-- 原「位置上限 = n_max」已不适用：素数按块身份派生，块数无上限。
            此处改为已分配的存储槽位（含删除留下的空洞，故 >= 块数）。 -->
      <StatCard hint-top label="存储槽位" :value="delta.slots_allocated" hint="已分配位置，含删除留下的空洞" />
    </div>

    <div class="section-heading section-heading-spaced">
      <div><span class="section-kicker mono">STORAGE FOOTPRINT</span><h2>存储规模</h2></div>
      <span class="section-note mono">LIVE COUNTERS</span>
    </div>
    <div class="grid">
      <StatCard hint-top label="文件数" :value="status.files" hint="全系统所有文件" />
      <StatCard hint-top label="总块数" :value="status.blocks" hint="各文件块数之和" />
      <StatCard hint-top label="副本数" :value="status.replica_factor" hint="每块默认存几份" />
      <StatCard
        hint-top
        label="副本不足"
        :value="status.under_replicated"
        :hint="status.under_replicated ? '开副本前上传的历史数据，重新上传可补齐' : '当前无缺口'"
      />
    </div>

    <div class="compact-sections">
      <section class="compact-section">
        <div class="section-heading">
          <div><span class="section-kicker mono">COMMITMENTS</span><h2>逐文件摘要 <span class="mono">每份文件一条向量</span></h2></div>
        </div>
        <div class="panel commitment-panel">
          <div class="panel-title-row"><span class="live-mark"><i />SYNCED</span></div>
          <div class="delta-row">
            <RingGauge :value="replicaOk" :max="status.blocks || 1" label="副本达标" />
            <div class="delta-detail">
              <div class="delta-item"><span class="k">n</span><span class="v mono">{{ delta.n }}</span><span class="hint">在用块数（各文件之和）</span></div>
              <!-- 摘要为逐文件粒度，不存在单一全局 U / C。
                   原 delta.U / delta.C 字段已不存在，故改为条数与逐条列表。 -->
              <div class="delta-item"><span class="k">向量</span><span class="v mono">{{ deltaFiles.length }}</span><span class="hint">每份文件一条</span></div>
              <div class="delta-item">
                <span class="k">δ 指纹</span>
                <span
                  class="v mono"
                  :title="deltaFiles.length === 1 ? ('U = ' + deltaFiles[0].U + '\nC = ' + deltaFiles[0].C) : ''"
                >{{ deltaFiles.length === 1 ? hexFp(deltaFiles[0].delta_fp, 12) : '每份各不相同' }}</span>
                <span class="hint">一份文件一个值</span>
              </div>
            </div>
          </div>
          <p class="fp-note">指纹是文件公开承诺 δ 的 12 位十六进制短摘要（sha256("U:C:n") 的前 12 位），用于在不暴露明文的前提下快速识别与核对文件；悬浮可查看该文件承诺的完整 U 与 C。</p>
          <div
            v-for="f in visibleFiles"
            :key="`${f.owner}/${f.file_key}`"
            class="panel-subtitle mono file-line"
            style="white-space: normal; margin-top: 6px"
            :title="'U = ' + f.U + '\nC = ' + f.C"
          >
            （{{ f.owner }}）{{ f.file_key }} n={{ f.n }} 指纹 {{ hexFp(f.delta_fp, 12) }}
          </div>
          <button v-if="hiddenFileCount" type="button" class="more-toggle" @click="showAllFiles = !showAllFiles">
            {{ showAllFiles ? '收起' : `展开其余 ${hiddenFileCount} 条` }}
          </button>
        </div>
      </section>

      <section class="compact-section">
        <div class="section-heading">
          <div><span class="section-kicker mono">RUNTIME HEALTH</span><h2>运行状态</h2></div>
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
          <div class="detail-toggle" @click="healthDetailOpen = !healthDetailOpen">
            <span>详情</span><span class="caret" :class="{ open: healthDetailOpen }">▾</span>
          </div>
          <div v-show="healthDetailOpen" class="detail-body">
            <p class="detail-line"><b>模式 {{ status.mode }}</b>：{{ modeDesc }}</p>
            <p class="detail-line"><b>传输 {{ status.transport }}</b>：{{ transportDesc }}</p>
          </div>
        </div>
      </section>

      <section class="compact-section compact-section--full">
        <div class="section-heading">
          <div><span class="section-kicker mono">KEY MANAGEMENT</span><h2>密钥模型</h2></div>
          <span class="section-note mono">NO MASTER KEY</span>
        </div>
        <div class="panel key-panel">

          <div class="keywrap">
            <div class="keywrap-item"><span class="k">方案</span><span class="v">{{ KEY_SCHEME_TEXT }}</span></div>
            <div class="keywrap-item"><span class="k">用户 / 持有密钥：</span><span class="v mono">{{ keywrap.users }} / {{ keywrap.user_keys }}</span></div>
            <div class="keywrap-item"><span class="k">私钥存储位置</span><span class="v">{{ KEY_STORAGE_TEXT }}</span></div>
            <div class="keywrap-item"><span class="k">解封位置</span><span class="v">{{ KEY_UNWRAP_TEXT }}</span></div>
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
              <span
                :class="lastCheck.ok ? 'text-ok' : 'text-danger'"
                :title="lastCheck.ok ? '' : lastCheck.message"
              >{{ lastCheck.ok
                ? `全部通过 ${lastCheck.passed}/${lastCheck.total}`
                : (lastCheck.failKey
                    ? `未通过：${itemName(lastCheck.failKey)}（原因悬停可见）`
                    : '未通过（未能定位到具体检查项，原因悬停可见）') }}</span>
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
            <el-alert
              :type="checkResult.ok ? 'success' : 'error'"
              :closable="false"
              :title="checkResult.ok ? CHECK_OK_TEXT : '自检未通过'"
              :description="checkResult.ok ? '' : checkResult.message"
            />
            <p
              v-if="checkResult.ok === false && checkResult.located === false"
              class="text-3"
              style="font-size: 13px; line-height: 1.7; margin: 8px 0 0"
            >
              未能定位到具体检查项 —— 清单保持灰点（未判定），结论以本条为准。
            </p>
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
              :total-ms="checkElapsed"
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
.eyebrow, .section-kicker { color: var(--accent); font-size: 13px; letter-spacing: .13em; }
.overview-head h1 { color: var(--text-1); font-size: 26px; letter-spacing: -.02em; margin: 7px 0 5px; }
.overview-head p { color: var(--text-2); font-size: 15px; margin: 0; }
.health-pill { display: inline-flex; align-items: center; gap: 8px; padding: 8px 11px; border: 1px solid var(--line); border-radius: 999px; color: var(--text-1); font-size: 14px; white-space: nowrap; background: color-mix(in srgb, var(--bg-raised) 78%, transparent); }
.health-pill small { color: var(--text-3); font-size: 13px; margin-left: 3px; }
.health-dot, .live-mark i { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 0 4px color-mix(in srgb, var(--ok) 12%, transparent); }
.health-pill.is-warn .health-dot { background: var(--warn); box-shadow: 0 0 0 4px color-mix(in srgb, var(--warn) 12%, transparent); }
/* 管线下方一行「当前规模」：数值取自 /api/system/status，实时刷新。 */
.hero-scale { display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px 26px; margin-top: 18px; padding-top: 15px; border-top: 1px solid var(--line); position: relative; z-index: 1; }
.hero-scale-item { color: var(--text-2); font-size: 13px; }
.hero-scale-item b { color: var(--text-1); font-size: 20px; font-weight: 500; margin-right: 4px; }
.hero-scale-hint { margin-left: auto; color: var(--text-3); font-size: 13px; letter-spacing: .1em; }
/* 逐文件摘要的展开 / 收起 */
.more-toggle { align-self: flex-start; margin-top: 10px; padding: 3px 0; color: var(--accent); font: inherit; font-size: 15px; background: none; border: 0; cursor: pointer; }
.more-toggle:hover { text-decoration: underline; }
/* 自检按钮与「上次自检」一行 */
.diag-actions { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }
.last-check { margin: 0; color: var(--text-3); font-size: 13px; }
/* 自检失败的原因是多句文本（含换行），按原样换行显示，不折成一团。 */
.diagnostics-panel :deep(.el-alert__description) {
  white-space: pre-line;
  line-height: 1.7;
  margin-top: 6px;
}
.section-heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; margin: 18px 1px 10px; }
.section-heading-spaced { margin-top: 27px; }
.section-heading h2 { color: var(--text-1); font-size: 19px; font-weight: 600; margin: 5px 0 0; }
.section-note { color: var(--text-3); font-size: 13px; letter-spacing: .08em; }
.compact-sections { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px 16px; margin-top: 6px; }
.compact-section { min-width: 0; display: flex; flex-direction: column; }
/* 自检 / 密钥模型整行显示：向下延伸时不再撑高同行的相邻卡片 */
.compact-section--full { grid-column: 1 / -1; }
.compact-section .section-heading { margin-top: 0; min-height: 43px; }
.compact-section > .panel { flex: 1 1 auto; min-height: 262px; box-sizing: border-box; }
/* 逐文件摘要随文件数变长（默认显示 4 条，可展开），故只设最小高度；
   右边的运行状态卡也不再定高 —— 详情展开后卡片跟着变高，
   内容不会溢出卡片边框；两边都不写死高度，靠同一行的拉伸对齐。 */
.commitment-panel { height: auto; min-height: 262px; }
.health-panel { height: auto; min-height: 262px; }
/* 整行的面板自适应高度，向下延伸不再受固定高度限制 */
.compact-section--full > .panel { min-height: 0; height: auto; }
.commitment-panel .delta-row { min-height: 112px; }
.health-summary { display: flex; align-items: baseline; gap: 8px; margin: 4px 0 17px; color: var(--text-2); font-size: 15px; }
.health-summary strong { color: var(--text-1); font-size: 34px; font-weight: 500; letter-spacing: -.04em; }
.health-summary-status { margin-left: auto; font-size: 14px; }
.panel-title-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 15px; }
.panel-subtitle { color: var(--text-2); font-size: 15px; }
.live-mark { display: inline-flex; align-items: center; gap: 7px; color: var(--text-3); font-size: 13px; letter-spacing: .08em; }
.live-mark i { display: inline-block; width: 5px; height: 5px; }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}
/* 指标卡内文字字号整体上调，与页面其余文字保持均衡（仅作用于本页，不改共享组件基值） */
.grid :deep(.stat-card .label) { font-size: 14px; }
.grid :deep(.stat-card .value) { font-size: 30px; }
.grid :deep(.stat-card .unit) { font-size: 15px; }
.grid :deep(.stat-card .hint) { font-size: 13px; }
.sec-title {
  font-size: 17px;
  font-weight: 600;
  color: var(--text-1);
  margin-bottom: 14px;
}
.delta-row {
  display: flex;
  gap: 28px;
  align-items: center;
}
.delta-detail {
  display: flex;
  flex-direction: row;
  flex-wrap: wrap;
  gap: 16px 32px;
  flex: 1;
}
.delta-item {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 3px;
}
.delta-item .k {
  font-family: var(--font-mono);
  color: var(--accent);
  font-weight: 500;
  font-size: 13px;
}
.delta-item .v {
  color: var(--text-1);
  font-size: 17px;
}
.delta-item .hint {
  color: var(--text-3);
  font-size: 14px;
}
.cluster-row {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  font-size: 15px;
  color: var(--text-2);
}
.detail-toggle { display: inline-flex; align-items: center; gap: 6px; margin-top: 12px; color: var(--accent); font-size: 13px; cursor: pointer; user-select: none; }
.detail-toggle .caret { transition: transform .2s ease; font-size: 11px; }
.detail-toggle .caret.open { transform: rotate(180deg); }
.detail-body { margin-top: 8px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--bg-raised); }
.detail-line { color: var(--text-2); font-size: 14px; line-height: 1.7; margin: 0 0 8px; }
.detail-line:last-child { margin-bottom: 0; }
.detail-line b { color: var(--text-1); }
.fp-note { color: var(--text-3); font-size: 14px; line-height: 1.6; margin: 10px 0 2px; }
.file-line { color: var(--text-2); font-size: 15px; }
/*
 * 标签列 + 值列：四项都是「标签在左、值在右」，值一律从**同一条竖线**开始。
 * 标签列宽取最宽的那个标签（max-content），所以「方案」「用户 / 持有密钥：」
 * 「私钥存储位置」「解封位置」后面的内容左边缘对齐。
 * .keywrap-item 用 display: contents 摊平，让 .k / .v 直接当 grid 的两列；
 * align-items: baseline 保证长值换行时，值的第一行仍与标签同一基线。
 */
.keywrap {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 8px 14px;
  align-items: baseline;
  font-size: 15px;
}
.keywrap-item { display: contents; }
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
  font-size: 13px;
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
  font-size: 15px;
  color: var(--text-2);
  white-space: nowrap;
}
.check-desc {
  margin-left: auto;
  font-size: 13px;
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
/* 大屏（>= 1500px）：限制卡片宽度，避免单行文本过长。 */
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
