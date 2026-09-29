<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { systemApi } from '../../../api/system'
import PageHeader from '../../../components/common/PageHeader.vue'

const summary = ref(null)
const error = ref('')
const loading = ref(false)
const lastAt = ref('')
const now = ref(Date.now())
const refreshMs = ref(5000)
const activeMetric = ref('latency')
const latencyPercentile = ref('p95')
const slaMs = ref(100)
const payloadKb = ref(64)
const concurrency = ref(4)
const verifyRatio = ref(30)
const isLive = ref(true)
let timer = 0
let clock = 0

const intervals = [
  { label: '5 秒（实时）', value: 5000 },
  { label: '15 秒', value: 15000 },
  { label: '1 分钟', value: 60000 },
  { label: '手动刷新', value: 0 },
]
const PRIMARY = ['upload', 'query', 'verify_batch', 'append', 'modify']
const colors = { upload: '#22d3ee', query: '#818cf8', verify_batch: '#f59e0b', append: '#22c55e', modify: '#f472b6' }

async function fetchSummary() {
  loading.value = true
  try {
    const { data } = await systemApi.perfSummary()
    summary.value = data
    error.value = ''
    lastAt.value = new Date(data.snapshot_at * 1000).toLocaleTimeString('zh-CN', { hour12: false })
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '获取性能快照失败'
  } finally {
    loading.value = false
  }
}
function startTimer() {
  stopTimer()
  isLive.value = refreshMs.value > 0
  if (refreshMs.value > 0) timer = window.setInterval(fetchSummary, refreshMs.value)
}
function stopTimer() { if (timer) { clearInterval(timer); timer = 0 } }
watch(refreshMs, startTimer)
onMounted(() => { fetchSummary(); startTimer(); clock = window.setInterval(() => { now.value = Date.now() }, 1000) })
onBeforeUnmount(() => { stopTimer(); clearInterval(clock) })

const names = computed(() => summary.value?.names || {})
const label = (key) => names.value[key] || key
const latencies = computed(() => summary.value?.latency || {})
const samples = computed(() => Object.values(latencies.value).reduce((n, x) => n + (x.count || 0), 0))
const totalOpsPerMin = computed(() => Object.values(summary.value?.throughput || {}).reduce((n, x) => n + x * 60, 0))
const queryLatency = computed(() => latencies.value.query || { p50: 0, p95: 0, p99: 0, count: 0 })
const health = computed(() => queryLatency.value.count ? queryLatency.value[latencyPercentile.value] <= slaMs.value : null)
const age = computed(() => summary.value?.snapshot_at ? Math.max(0, Math.round(now.value / 1000 - summary.value.snapshot_at)) : null)
const scale = computed(() => summary.value?.scale || {})
const crs = computed(() => summary.value?.crs || {})
const operationRows = computed(() => PRIMARY.filter(k => latencies.value[k]?.count).map(k => ({ key: k, name: label(k), ...latencies.value[k], throughput: Math.round((summary.value?.throughput?.[k] || 0) * 60) })))

const series = computed(() => (summary.value?.series || []).slice(-24))
const chart = computed(() => {
  const points = series.value
  if (!points.length) return null
  const key = activeMetric.value === 'throughput' ? '_throughput' : latencyPercentile.value
  const values = points.map(p => activeMetric.value === 'throughput'
    ? PRIMARY.reduce((n, k) => n + (p[`${k}_n`] || 0), 0) * 6
    : PRIMARY.reduce((n, k) => n + (p[k] || 0), 0) / Math.max(1, PRIMARY.filter(k => p[k] != null).length))
  const W = 760, H = 240, L = 48, R = 16, T = 18, B = 30
  const max = Math.max(activeMetric.value === 'throughput' ? 10 : slaMs.value, ...values, 1) * 1.18
  const x = i => L + (i / Math.max(1, values.length - 1)) * (W - L - R)
  const y = v => T + (1 - v / max) * (H - T - B)
  const line = values.map((v, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ')
  const area = `${line} L${x(values.length - 1)},${H - B} L${x(0)},${H - B} Z`
  return { W, H, L, R, T, B, max, x, y, line, area, values, points, threshold: activeMetric.value === 'latency' ? slaMs.value : null }
})
const latestValue = computed(() => chart.value?.values.at(-1) || 0)

const projection = computed(() => {
  const base = Math.max(1, totalOpsPerMin.value || 240)
  const loadFactor = Math.min(2.8, 0.45 + concurrency.value * 0.17)
  const blockFactor = Math.max(0.62, 1.08 - payloadKb.value / 1000)
  const verifyFactor = 1 + verifyRatio.value / 260
  return { throughput: Math.round(base * loadFactor * blockFactor / verifyFactor), latency: Math.round(Math.max(1, (queryLatency.value.p50 || 28) * (0.78 + concurrency.value * 0.08) * (1 + payloadKb.value / 640) * verifyFactor)) }
})
const projectionBars = computed(() => [1, 2, 4, 8, 12].map(c => ({ c, value: Math.round(projection.value.throughput * Math.sqrt(c / concurrency.value)) })))
const fmt = (n) => Number(n || 0).toLocaleString('zh-CN')
const fmtTime = (t) => new Date(t * 1000).toLocaleTimeString('zh-CN', { hour12: false, minute: '2-digit', second: '2-digit' })
</script>

<template>
  <div class="perf-page">
    <PageHeader title="性能实验台" subtitle="实时性能与参数实验" />

    <div class="toolbar">
      <div class="live-status" :class="{ paused: !isLive }"><i /> {{ isLive ? '实时采样中' : '已暂停' }}</div>
      <span class="fresh">{{ lastAt ? `最新快照 ${lastAt} · ${age}s 前` : '等待首个快照' }}</span>
      <el-select v-model="refreshMs" size="small" style="width: 142px"><el-option v-for="o in intervals" :key="o.value" :label="o.label" :value="o.value" /></el-select>
      <el-button size="small" :loading="loading" @click="fetchSummary">刷新快照</el-button>
    </div>
    <el-alert v-if="error" type="error" :closable="false" class="mb-3" :title="error" />

    <section class="hero-grid">
      <div class="hero-panel">
        <div class="eyebrow">LIVE PERFORMANCE / {{ lastAt || '--:--:--' }}</div>
        <div class="hero-row"><div><div class="hero-value">{{ fmt(totalOpsPerMin) }}<span> ops/min</span></div><div class="hero-label">系统实时吞吐 <b>· 最近 60 秒</b></div></div><div class="health" :class="health === false ? 'bad' : ''"><span>{{ health === null ? '待采样' : health ? 'SLA 达标' : '尾延迟超标' }}</span><small>{{ latencyPercentile.toUpperCase() }} ≤ {{ slaMs }} ms</small></div></div>
        <div class="hero-meta"><span>采样数 <b>{{ fmt(samples) }}</b></span><span>在线节点 <b>{{ scale.nodes ?? '—' }}</b></span><span>当前文件 <b>{{ fmt(scale.files) }}</b></span><span>向量块 <b>{{ fmt(scale.blocks) }}</b></span></div>
      </div>
      <div class="scheme-panel"><div class="section-kicker">SCHEME CONTEXT</div><h3>可验证分布式存储</h3><p>性能结果与密码参数绑定，便于竞赛评审复现。</p><div class="scheme-grid"><span>CRS 位数<strong>{{ crs.n_bits || '—' }}</strong></span><span>向量维度<strong>{{ crs.l || '—' }}</strong></span><span>最大块数<strong>{{ crs.n_max || '—' }}</strong></span><span>数据来源<strong>运行时内存</strong></span></div></div>
    </section>

    <section class="kpi-grid">
      <div class="kpi"><span class="kpi-label">查询 P50</span><strong>{{ queryLatency.p50 }}<em>ms</em></strong><small>典型请求延迟</small></div>
      <div class="kpi"><span class="kpi-label">查询 P95</span><strong>{{ queryLatency.p95 }}<em>ms</em></strong><small>大多数请求上限</small></div>
      <div class="kpi"><span class="kpi-label">查询 P99</span><strong>{{ queryLatency.p99 }}<em>ms</em></strong><small>尾部延迟</small></div>
      <div class="kpi"><span class="kpi-label">最近 60 秒操作</span><strong>{{ fmt(Math.round(totalOpsPerMin / 60)) }}<em>次</em></strong><small>{{ fmt(samples) }} 个累计样本</small></div>
    </section>

    <section class="main-grid">
      <div class="panel chart-panel"><div class="panel-head"><div><div class="section-kicker">OBSERVABILITY</div><h2>实时性能趋势</h2></div><div class="segmented"><button :class="{ active: activeMetric === 'latency' }" @click="activeMetric = 'latency'">延迟</button><button :class="{ active: activeMetric === 'throughput' }" @click="activeMetric = 'throughput'">吞吐</button></div></div><div v-if="chart" class="chart-box"><svg :viewBox="`0 0 ${chart.W} ${chart.H}`" class="chart"><line v-for="i in 4" :key="i" :x1="chart.L" :x2="chart.W-chart.R" :y1="chart.y(chart.max*i/4)" :y2="chart.y(chart.max*i/4)" class="grid" /><text v-for="i in 4" :key="'y'+i" :x="chart.L-8" :y="chart.y(chart.max*i/4)+4" text-anchor="end" class="axis">{{ Math.round(chart.max*i/4) }}</text><line v-if="chart.threshold" :x1="chart.L" :x2="chart.W-chart.R" :y1="chart.y(chart.threshold)" :y2="chart.y(chart.threshold)" class="sla-line" /><path :d="chart.area" class="chart-area" /><path :d="chart.line" class="chart-line" /><circle v-for="(v, i) in chart.values" :key="i" :cx="chart.x(i)" :cy="chart.y(v)" r="3.2" class="point"><title>{{ fmtTime(chart.points[i].t) }} · {{ v.toFixed(1) }}</title></circle></svg><div class="chart-foot"><span>{{ chart.points.length ? fmtTime(chart.points[0].t) : '' }}</span><span class="chart-now">● {{ activeMetric === 'latency' ? `平均 ${latestValue.toFixed(1)} ms · SLA ${slaMs} ms` : `当前 ${Math.round(latestValue)} ops/min` }}</span><span>{{ chart.points.length ? fmtTime(chart.points.at(-1).t) : '' }}</span></div></div><div v-else class="empty">还没有样本。做几次上传或查询，趋势就出来了。</div></div>
      <div class="panel summary-panel"><div class="section-kicker">TAIL LATENCY</div><h2>延迟分布</h2><div class="percentile-tabs"><button v-for="p in ['p50','p95','p99']" :key="p" :class="{ active: latencyPercentile === p }" @click="latencyPercentile = p"><b>{{ (queryLatency[p] || 0).toFixed(1) }}</b><span>{{ p.toUpperCase() }} / ms</span></button></div><div class="sla-control"><div><span>SLA 阈值</span><b>{{ slaMs }} ms</b></div><el-slider v-model="slaMs" :min="20" :max="500" :step="10" /></div><div class="bar-row" v-for="row in operationRows.slice(0, 4)" :key="row.key"><span>{{ row.name }}</span><div><i :style="{ width: `${Math.min(100, row[latencyPercentile] / Math.max(slaMs, 1) * 100)}%`, background: colors[row.key] }" /></div><b>{{ row[latencyPercentile] }} ms</b></div></div>
    </section>

    <section class="experiment-grid"><div class="panel controls-panel"><div class="section-kicker">WHAT-IF BENCHMARK</div><h2>参数实验</h2><p class="muted">调参数，看预估值怎么变（基线取实时快照）。</p><label>消息块大小 <b>{{ payloadKb }} KB</b><el-slider v-model="payloadKb" :min="4" :max="256" :step="4" /></label><label>并发请求 <b>{{ concurrency }} 路</b><el-slider v-model="concurrency" :min="1" :max="16" /></label><label>验证比例 <b>{{ verifyRatio }}%</b><el-slider v-model="verifyRatio" :min="0" :max="100" :step="5" /></label><div class="estimate-row"><div><small>预估吞吐</small><strong>{{ fmt(projection.throughput) }}<em> ops/min</em></strong></div><div><small>预估查询延迟</small><strong>{{ projection.latency }}<em> ms</em></strong></div></div></div><div class="panel scale-panel"><div class="panel-head"><div><div class="section-kicker">CONCURRENCY SWEEP</div><h2>并发敏感性</h2></div><span class="tag">预估模型</span></div><div class="sweep"><div v-for="bar in projectionBars" :key="bar.c" class="sweep-col"><div class="sweep-value">{{ fmt(bar.value) }}</div><div class="sweep-track"><i :style="{ height: `${Math.min(100, bar.value / Math.max(...projectionBars.map(x => x.value)) * 100)}%` }" /></div><span>{{ bar.c }} 路</span></div></div><div class="sweep-note">输入：消息块 {{ payloadKb }} KB · 验证比例 {{ verifyRatio }}%</div></div></section>

    <section class="panel table-panel"><div class="panel-head"><div><div class="section-kicker">OPERATION BREAKDOWN</div><h2>操作明细</h2></div><span class="muted">分位数来自最近 {{ fmt(samples) }} 个运行样本</span></div><el-table :data="operationRows" size="small"><el-table-column prop="name" label="操作" width="150" /><el-table-column prop="count" label="样本" width="100" /><el-table-column label="P50 / ms"><template #default="{ row }">{{ row.p50 }}</template></el-table-column><el-table-column label="P95 / ms"><template #default="{ row }">{{ row.p95 }}</template></el-table-column><el-table-column label="P99 / ms"><template #default="{ row }">{{ row.p99 }}</template></el-table-column><el-table-column prop="throughput" label="最近吞吐 / 分" /></el-table><p v-if="!operationRows.length" class="empty">尚无运行样本。请先执行上传、查询或验证操作。</p></section>
    <p class="footnote">数据来自后端进程内的运行采样。“参数实验”是按当前样本估算的，不算实测成绩。</p>
  </div>
</template>

<style scoped>
.perf-page { max-width: 1480px; margin: 0 auto; padding-bottom: 24px; }
.toolbar { display:flex; align-items:center; gap:10px; margin:-8px 0 16px; }.fresh,.muted,.footnote { color:var(--text-3); font-size:12px; }.fresh { margin-right:auto; }.live-status { display:flex; align-items:center; gap:7px; color:var(--ok); font-size:12px; font-weight:600; }.live-status i { width:7px; height:7px; border-radius:50%; background:var(--ok); box-shadow:0 0 0 4px color-mix(in srgb,var(--ok),transparent 82%); animation:pulse 1.8s infinite; }.live-status.paused { color:var(--text-3); }.live-status.paused i { background:var(--text-3); animation:none; }
.hero-grid,.main-grid,.experiment-grid { display:grid; gap:14px; margin-bottom:14px; }.hero-grid { grid-template-columns: minmax(0,1.55fr) minmax(300px,1fr); }.main-grid { grid-template-columns:minmax(0,1.65fr) minmax(300px,1fr); }.experiment-grid { grid-template-columns: minmax(300px,.85fr) minmax(0,1.15fr); }.hero-panel,.scheme-panel,.panel,.kpi { background:var(--bg-panel); border:1px solid var(--line); border-radius:var(--radius); }.hero-panel { padding:24px; background:linear-gradient(135deg,var(--bg-panel),color-mix(in srgb,var(--accent-2),var(--bg-panel) 92%)); }.scheme-panel,.panel { padding:18px; }.eyebrow,.section-kicker { color:var(--accent); font:600 10px var(--font-mono); letter-spacing:.12em; }.hero-row { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; margin:20px 0 24px; }.hero-value { font:600 clamp(34px,4vw,58px) var(--font-mono); color:var(--text-1); letter-spacing:0; }.hero-value span { font:500 14px var(--font-sans); color:var(--text-2); }.hero-label { color:var(--text-2); font-size:13px; }.hero-label b { color:var(--text-3); font-weight:400; }.health { min-width:106px; border-left:1px solid var(--line); padding-left:16px; color:var(--ok); }.health.bad { color:var(--danger); }.health span { display:block; font-size:13px; font-weight:600; }.health small { display:block; color:var(--text-3); margin-top:5px; }.hero-meta,.scheme-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:10px; }.hero-meta span,.scheme-grid span { color:var(--text-3); font-size:11px; }.hero-meta b,.scheme-grid strong { display:block; color:var(--text-1); font:500 15px var(--font-mono); margin-top:5px; }.scheme-panel h3 { margin:12px 0 6px; font-size:18px; }.scheme-panel p { margin:0 0 22px; color:var(--text-2); font-size:12px; }.scheme-grid { grid-template-columns:repeat(2,1fr); }.scheme-grid strong { font-size:13px; }
.kpi-grid { display:grid; grid-template-columns:repeat(4,1fr); gap:14px; margin-bottom:14px; }.kpi { padding:16px 18px; }.kpi-label { color:var(--text-2); font-size:12px; }.kpi strong { display:block; margin:8px 0 4px; font:600 28px var(--font-mono); }.kpi em,.estimate-row em { color:var(--text-3); font:400 12px var(--font-sans); }.kpi small { color:var(--text-3); font-size:11px; }.panel-head { display:flex; justify-content:space-between; align-items:flex-start; gap:12px; }.panel h2 { margin:6px 0 18px; font-size:16px; font-weight:600; }.segmented,.percentile-tabs { display:flex; gap:4px; }.segmented button,.percentile-tabs button { border:1px solid var(--line); background:transparent; color:var(--text-2); cursor:pointer; }.segmented button { padding:6px 12px; border-radius:5px; font-size:12px; }.segmented button.active,.percentile-tabs button.active { background:color-mix(in srgb,var(--accent),transparent 86%); border-color:var(--accent); color:var(--accent); }.chart-box { min-height:250px; }.chart { width:100%; height:auto; min-height:230px; overflow:visible; }.grid { stroke:var(--line); stroke-width:1; }.axis { fill:var(--text-3); font-size:10px; }.chart-line { fill:none; stroke:var(--accent); stroke-width:2.5; stroke-linecap:round; stroke-linejoin:round; filter:drop-shadow(0 0 5px color-mix(in srgb,var(--accent),transparent 55%)); }.chart-area { fill:color-mix(in srgb,var(--accent),transparent 90%); }.point { fill:var(--bg-panel); stroke:var(--accent); stroke-width:2; }.sla-line { stroke:var(--warn); stroke-dasharray:5 5; }.chart-foot { display:flex; justify-content:space-between; color:var(--text-3); font:10px var(--font-mono); }.chart-now { color:var(--accent); }.percentile-tabs { margin-bottom:24px; }.percentile-tabs button { flex:1; text-align:left; padding:10px; border-radius:5px; }.percentile-tabs b,.percentile-tabs span { display:block; }.percentile-tabs b { font:600 20px var(--font-mono); }.percentile-tabs span { margin-top:4px; font-size:10px; }.sla-control { padding:12px 0 14px; border-top:1px solid var(--line); }.sla-control div { display:flex; justify-content:space-between; color:var(--text-2); font-size:12px; }.sla-control b { color:var(--warn); font:500 12px var(--font-mono); }.bar-row { display:grid; grid-template-columns:64px 1fr 58px; gap:10px; align-items:center; margin:13px 0; font-size:11px; color:var(--text-2); }.bar-row div { height:5px; background:var(--bg-raised); border-radius:4px; overflow:hidden; }.bar-row i { display:block; height:100%; border-radius:4px; transition:width .45s ease; }.bar-row b { text-align:right; font:500 11px var(--font-mono); color:var(--text-1); }.empty { padding:40px 8px; text-align:center; color:var(--text-3); font-size:12px; }
.controls-panel label { display:block; color:var(--text-2); font-size:12px; margin:18px 0; }.controls-panel label b { float:right; color:var(--accent); font:500 12px var(--font-mono); }.estimate-row { display:grid; grid-template-columns:1fr 1fr; gap:10px; margin-top:20px; }.estimate-row div { padding:12px; background:var(--bg-raised); border:1px solid var(--line); border-radius:5px; }.estimate-row small { display:block; color:var(--text-3); font-size:11px; }.estimate-row strong { display:block; margin-top:6px; color:var(--text-1); font:600 20px var(--font-mono); }.tag { border:1px solid color-mix(in srgb,var(--warn),transparent 50%); color:var(--warn); border-radius:4px; padding:3px 7px; font-size:10px; }.sweep { display:flex; align-items:flex-end; justify-content:space-around; height:210px; padding:10px 20px 0; border-bottom:1px solid var(--line); }.sweep-col { height:100%; display:flex; flex-direction:column; justify-content:flex-end; align-items:center; gap:6px; color:var(--text-3); font:10px var(--font-mono); }.sweep-value { color:var(--text-2); font-size:10px; }.sweep-track { width:30px; height:145px; display:flex; align-items:flex-end; background:var(--bg-raised); border-radius:3px 3px 0 0; overflow:hidden; }.sweep-track i { width:100%; background:linear-gradient(to top,var(--accent-2),var(--accent)); border-radius:3px 3px 0 0; transition:height .35s ease; }.sweep-note { color:var(--text-3); font-size:11px; margin-top:12px; }.table-panel { margin-bottom:12px; }.table-panel .panel-head { margin-bottom:4px; }.footnote { line-height:1.7; }
@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.4; } }
@media (max-width:1000px) { .hero-grid,.main-grid,.experiment-grid { grid-template-columns:1fr; }.kpi-grid { grid-template-columns:repeat(2,1fr); } }
@media (max-width:600px) { .toolbar { flex-wrap:wrap; }.fresh { order:3; width:100%; }.hero-meta { grid-template-columns:repeat(2,1fr); }.hero-row { flex-direction:column; }.kpi-grid { grid-template-columns:1fr 1fr; gap:8px; }.kpi { padding:12px; }.kpi strong { font-size:22px; }.scheme-panel,.panel,.hero-panel { padding:14px; } }
</style>
