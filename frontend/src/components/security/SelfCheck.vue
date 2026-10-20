<script setup>
/**
 * 系统自检 —— 逐项核对集群与向量一致性（后端 ``POST /api/admin/check``，只读）。
 *
 * ★ 2026-10-19：从「一小块 + 弹框」改回**页面上的一组清单**，挂进
 *   **管理员工作台 → 初始化与系统自检**：那一页的按钮点下去，初始化流水线与
 *   这十项核对**一起**跑，跑完页面才把系统参数等区块展开。
 *
 *   · 组件**不承担页面级按钮**：跑一次由父页面（init/index.vue）拿 ref 调 ``run()``；
 *     “再次自检”这颗小按钮留在清单右上角，可以反复跑。
 *   · 十项逐项点亮 + 进度条 + 结论 + 耗时，全部直接摆在页面上；
 *   · 上一次的结论与逐项状态存在本机（``vds_last_check_v1``），重进页面时
 *     直接还原 —— 不必为了看一眼结论再扫一遍。
 *
 * 后端是一次调用返回全部结论，逐项动画是**揭示过程**而非真实分次请求 ——
 * 所以那个耗时取的是**真实的一次请求耗时**，不是动画时长。
 */
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { systemApi } from '../../api/system'
import StageTimeline from './StageTimeline.vue'

const props = defineProps({
  /** 挂载时是否还原本机上一次的结果：重进页面要，点按钮开跑时不要（否则会闪一下旧结论）。 */
  restore: { type: Boolean, default: true },
})

/** 逐项清单：与后端 ``store.check()`` 的检查项一一对应。 */const CHECK_ITEMS = [
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

/** 自检失败时，按关键词把后端报错定位到具体检查项。 */
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

const CHECK_OK_TEXT = '自检通过：登记表 / 节点视图 / 增量摘要与一次性承诺一致'

/* ---------------------------------------------------------------------------
   上次自检的结论（存在**本机浏览器**里）
   ---------------------------------------------------------------------------
   后端没有"自检历史"接口，审计流水又只有管理员看得到 —— 想知道"上一次查是
   什么时候、结果如何"，只能靠本机记一笔。所以文案就写"本机上次自检"，
   不假装它是服务端记录。清理浏览器数据就会没，不影响自检本身。
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

const checking = ref(false)
const checkResult = ref(null)
const checkSteps = ref([])
/** 自检的端到端耗时（毫秒）：从发起请求到它返回的墙钟差值。 */
const checkElapsed = ref(0)

const passedCount = computed(() => checkSteps.value.filter((s) => s.state === 'pass').length)

// 逐项扫描节奏：每项约 0.71 秒，10 项合计约 7.1 秒。
const SCAN_MS = 713
const STEP_GAP_MS = 75

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

function locateFailKey(message) {
  for (const it of FAIL_LOCATE) if (it.re.test(message)) return it.key
  return null
}

function itemName(key) {
  return CHECK_ITEMS.find((x) => x.key === key)?.name || key
}

/** 「本机上次自检」的相对时间（不足一分钟按“刚刚”）。 */
function fmtAgo(ts) {
  const sec = Math.max(0, Math.round((Date.now() - Number(ts || 0)) / 1000))
  if (sec < 60) return '刚刚'
  if (sec < 3600) return `${Math.round(sec / 60)} 分钟前`
  if (sec < 86400) return `${Math.round(sec / 3600)} 小时前`
  return `${Math.round(sec / 86400)} 天前`
}

// ★ 相对时间要跟着走：页面上显示的是「刚刚 / N 分钟前」，
//   不刷新的话它会一直停在打开那一刻的说法。**卸载时要清掉**，
//   否则每次切进切出都留下一个定时器。
const now = ref(Date.now())
let agoTimer = 0
onMounted(() => {
  agoTimer = window.setInterval(() => {
    now.value = Date.now()
  }, 30000)
})
onBeforeUnmount(() => {
  if (agoTimer) clearInterval(agoTimer)
  agoTimer = 0
})

const agoText = computed(() => {
  void now.value
  return lastCheck.value ? fmtAgo(lastCheck.value.at) : ''
})

async function run() {
  if (checking.value) return
  checking.value = true
  checkResult.value = null
  checkElapsed.value = 0
  checkSteps.value = CHECK_ITEMS.map((it) => ({ ...it, state: 'pending' }))

  // 真实自检在后台整体执行（一次请求、十项一起查），动画过程逐项揭示。
  const t0 = performance.now()
  const real = systemApi
    .check()
    .then((d) => {
      checkElapsed.value = Math.round(performance.now() - t0)
      return { ok: true, data: d.data }
    })
    .catch((e) => ({ ok: false, message: e?.response?.data?.detail || e?.message || '自检失败' }))

  // ★★ 结果未知时只亮「正在核对」，**绝不先标成 pass**。
  //   上一版是「先逐个亮绿、等请求回来再改红」—— 节点掉线时十项会先全部显示通过，
  //   随后才把第 3 项刷成失败，看的人只记住了那一片绿（用户 2026-10-20 报的就是这个）。
  //   现在先等真实结论，再按结论逐项揭示：通过→绿、失败→红、未判定→灰。
  checkSteps.value[0].state = 'running'
  const r = await real

  // 真实结论 → 每一项的最终状态（与旧逻辑同一套判定，只是提前算好再揭示）。
  let finalStates
  if (r.ok) {
    finalStates = CHECK_ITEMS.map(() => 'pass')
  } else {
    const failKey = locateFailKey(r.message)
    const failIdx = failKey ? CHECK_ITEMS.findIndex((x) => x.key === failKey) : -1
    finalStates = CHECK_ITEMS.map((_, i) => {
      if (failIdx >= 0 && i < failIdx) return 'pass'
      if (i === failIdx) return 'fail'
      return 'pending' // 未判定：灰点（不再冒充已通过）
    })
  }

  for (let i = 0; i < CHECK_ITEMS.length; i++) {
    const step = checkSteps.value[i]
    step.state = 'running'
    await sleep(SCAN_MS)
    step.state = finalStates[i]
    await sleep(STEP_GAP_MS)
  }

  if (r.ok) {
    checkResult.value = r.data
    saveLastCheck({
      at: Date.now(),
      ok: true,
      passed: CHECK_ITEMS.length,
      total: CHECK_ITEMS.length,
      message: CHECK_OK_TEXT,
      states: checkSteps.value.map((s) => s.state),
      elapsed: checkElapsed.value,
      timings: r.data?.timings || null,
      notes: r.data?.notes || [],
    })
  } else {
    // 定位得到就标出那一项；定位不到就**什么都不标**。
    //   为什么不再默认冤枉最后一项（原来 failIdx 缺省为 length-1）：后端是
    //   **一次调用**返回首个问题，而任何没被规则覆盖的原因（比如库读不出来）
    //   都会被按到「增量摘要一致性」头上 —— 那是在编一个看起来很像的结论。
    const failKey = locateFailKey(r.message)
    checkResult.value = { ok: false, message: r.message, located: !!failKey }
    saveLastCheck({
      at: Date.now(),
      ok: false,
      total: CHECK_ITEMS.length,
      failKey: failKey || '',
      message: r.message,
      states: checkSteps.value.map((s) => s.state),
      elapsed: checkElapsed.value,
    })
  }
  checking.value = false
}

/** 从本机记录还原上一次的结论与逐项状态（重进页面时不必再扫一遍）。 */
function restoreLastCheck() {
  const l = lastCheck.value
  if (!l || !Array.isArray(l.states) || !l.states.length) return
  checkSteps.value = CHECK_ITEMS.map((it, i) => ({ ...it, state: l.states[i] || 'pending' }))
  checkElapsed.value = Number(l.elapsed) || 0
  checkResult.value = l.ok
    ? { ok: true, timings: l.timings || null, notes: l.notes || [] }
    : {
        ok: false,
        message: l.message || '',
        located: !!l.failKey,
        timings: l.timings || null,
        notes: l.notes || [],
      }
}

if (props.restore) restoreLastCheck()

// ★ 跑一次的入口：父页面（初始化与系统自检）的按钮与右上角「再次自检」都调它。
defineExpose({ run })
/** 右上角那枚状态灯：扫描中 / 已完成 / 未开始。 */
const stateText = computed(() =>
  checking.value ? 'SCANNING' : checkSteps.value.length ? 'DONE' : 'IDLE',
)

/**
 * 收起后那一行摘要（用户 2026-10-19：十项全摆出来太长，跑完就收）。
 *
 * ★ 失败时把**出错项的名字**写进这一行 —— 收起不等于不告知哪里不对。
 */
const summaryText = computed(() => {
  const total = checkSteps.value.length
  if (!total) return ''
  const ms = checkElapsed.value ? ` · ${checkElapsed.value} ms` : ''
  if (checkResult.value?.ok) return `十项核对完成 · 通过 ${passedCount.value}/${total}${ms}`
  const failed = checkSteps.value.find((s) => s.state === 'fail')
  const who = failed ? `：${failed.name}` : lastCheck.value?.failKey ? `：${itemName(lastCheck.value.failKey)}` : ''
  return `核对未通过${who} · 通过 ${passedCount.value}/${total}${ms}`
})
</script>

<template>
  <div class="selfcheck">
    <div class="panel-title-row">
      <span class="panel-subtitle">逐项核对集群与向量一致性</span>
      <div class="sc-head-right">
        <span class="live-mark" :class="{ 'is-scan': checking }"><i />{{ stateText }}</span>
        <!-- ★ 自检可反复执行 —— 受「一次登录只能点一次」限制的是初始化，不是这里（用户 2026-10-19）。 -->
        <el-button size="small" :loading="checking" @click="run">再次自检</el-button>
      </div>
    </div>

    <!-- 十项清单只在**扫描过程中**铺开；跑完立刻收起，只留一行结论（用户 2026-10-19）。 -->
    <div v-if="checking" class="check-list">
      <div class="check-progress">
        <span class="mono check-progress-label">SCANNING {{ passedCount }} / {{ checkSteps.length }}</span>
        <div class="check-progress-bar"><span :style="{ width: (passedCount / checkSteps.length) * 100 + '%' }" /></div>
      </div>

      <div v-for="s in checkSteps" :key="s.key" class="check-item" :class="'is-' + s.state">
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
    <p v-else-if="checkSteps.length" class="sc-sum">
      <span class="mono sc-sum-tag" :class="checkResult?.ok ? 'is-ok' : 'is-bad'">
        {{ checkResult?.ok ? 'PASS' : 'FAIL' }}
      </span>
      <span>{{ summaryText }}</span>
    </p>
    <p v-else class="sc-hint">尚未自检 —— 十项会在「初始化与系统自检」点下后逐项亮起。</p>

    <div v-if="checkResult" class="check-verdict">
      <el-alert
        :type="checkResult.ok ? 'success' : 'error'"
        :closable="false"
        :title="checkResult.ok ? CHECK_OK_TEXT : '自检未通过'"
        :description="checkResult.ok ? '' : checkResult.message"
      />
      <p
        v-if="checkResult.ok === false && checkResult.located === false"
        class="text-3 sc-note"
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

    <p v-if="lastCheck && !checking" class="sc-ago mono">本机上次自检 · {{ agoText }}</p>
  </div>
</template>

<style scoped>
/* ---- 内嵌在「初始化与系统自检」页里的一组清单 ---- */
.selfcheck { display: flex; flex-direction: column; }
.sc-hint { color: var(--text-3); font-size: 13px; margin: 4px 0 0; }
.sc-head-right { display: flex; align-items: center; gap: 10px; }
.sc-sum { display: flex; align-items: center; gap: 9px; font-size: 13px; color: var(--text-2); margin: 2px 0 0; }
.sc-sum-tag { font-size: 11px; letter-spacing: .1em; padding: 1px 6px; border-radius: 4px; }
.sc-sum-tag.is-ok { color: var(--ok); background: color-mix(in srgb, var(--ok) 12%, transparent); }
.sc-sum-tag.is-bad { color: var(--danger); background: color-mix(in srgb, var(--danger) 14%, transparent); }
.sc-note { font-size: 13px; line-height: 1.7; margin: 8px 0 0; }
.sc-ago { color: var(--text-3); font-size: 12px; letter-spacing: .04em; margin: 10px 0 0; }

/* ---- 逐项扫描 ---- */
.panel-title-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.panel-subtitle { color: var(--text-2); font-size: 14px; }
.live-mark { display: inline-flex; align-items: center; gap: 7px; color: var(--text-3); font-size: 13px; letter-spacing: .08em; }
.live-mark i { display: inline-block; width: 5px; height: 5px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 0 4px color-mix(in srgb, var(--ok) 12%, transparent); }
.live-mark.is-scan i { background: var(--accent); box-shadow: 0 0 0 4px color-mix(in srgb, var(--accent) 15%, transparent); animation: pulse 1s ease-in-out infinite; }

.check-list { display: flex; flex-direction: column; gap: 6px; }
.check-progress { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
.check-progress-label { font-size: 13px; letter-spacing: .12em; color: var(--accent); white-space: nowrap; }
.check-progress-bar { flex: 1; height: 3px; border-radius: 2px; background: var(--bg-raised); overflow: hidden; }
.check-progress-bar span { display: block; height: 100%; background: linear-gradient(90deg, var(--accent), var(--accent-2)); border-radius: 2px; transition: width .3s ease; }
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
.check-state { width: 16px; height: 16px; flex: none; display: inline-flex; align-items: center; justify-content: center; }
.spinner {
  width: 13px;
  height: 13px;
  border: 2px solid color-mix(in srgb, var(--accent) 25%, transparent);
  border-top-color: var(--accent);
  border-radius: 50%;
  animation: spin .7s linear infinite;
}
.dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-3); }
.mark {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  line-height: 1;
  animation: pop .22s ease;
}
.mark-pass { color: var(--ok); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 14%, transparent); }
.mark-fail { color: var(--danger); box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger) 16%, transparent); }
.check-name { font-size: 14px; color: var(--text-2); white-space: nowrap; }
.check-desc { margin-left: auto; font-size: 13px; color: var(--text-3); text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
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
.check-verdict { margin-top: 14px; animation: fadein .3s ease; }
/* 自检失败的原因是**多句文本**（含换行），按原样换行显示，别折成一团。 */
.check-verdict :deep(.el-alert__description) { white-space: pre-line; line-height: 1.7; margin-top: 6px; }
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes scan { 0% { transform: translateX(-110%); } 100% { transform: translateX(330%); } }
@keyframes pop { 0% { transform: scale(.5); } 100% { transform: scale(1); } }
@keyframes fadein { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .35; } }
</style>
