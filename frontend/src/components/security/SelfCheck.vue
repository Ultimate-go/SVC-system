<script setup>
/**
 * 系统自检 —— 逐项核对集群与向量一致性（后端 ``POST /api/admin/check``，只读）。
 *
 * ★ 为什么是个组件（2026-10-16）：这一段原本是「总览」页上的一整节，
 *   标题 + 面板 + 十项清单占满一整行。现在搬到 **管理员工作台 → 系统初始化**，
 *   并且**收小**：
 *
 *   · 页面上只留**一小块**（一个按钮 + 一行"本机上次自检"），不占一整行；
 *   · 真正那十项扫描放到一个**弹框**里跑（进度条 + 逐项动效 + 结论 + 耗时）；
 *   · 自检完成后，页面上那一小块自己就变成了结论（✓ 全部通过 / ✕ 未通过）。
 *
 * 后端是一次调用返回全部结论，逐项动画是**揭示过程**而非真实分次请求 ——
 * 所以标题里那个耗时取的是**真实的一次请求耗时**，不是动画时长。
 */
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { systemApi } from '../../api/system'
import StageTimeline from './StageTimeline.vue'
import Icon from '../icons/Icon.vue'

/** 逐项清单：与后端 ``store.check()`` 的检查项一一对应。 */
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

const dialogOpen = ref(false)
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

function itemName(key) {
  return CHECK_ITEMS.find((x) => x.key === key)?.name || key
}

function locateFailKey(message) {
  for (const it of FAIL_LOCATE) if (it.re.test(message)) return it.key
  return null
}

/** 「本机上次自检」的相对时间（不足一分钟按“刚刚”）。 */
function fmtAgo(ts) {
  const sec = Math.max(0, Math.round((Date.now() - Number(ts || 0)) / 1000))
  if (sec < 60) return '刚刚'
  if (sec < 3600) return `${Math.round(sec / 60)} 分钟前`
  if (sec < 86400) return `${Math.round(sec / 3600)} 小时前`
  return `${Math.round(sec / 86400)} 天前`
}

/** 页面上那一小块里的结论文字。 */
const summaryText = computed(() => {
  const l = lastCheck.value
  if (!l) return ''
  return l.ok
    ? `全部通过 ${l.passed}/${l.total}`
    : l.failKey
      ? `未通过：${itemName(l.failKey)}`
      : '未通过（未能定位到具体检查项）'
})

// ★ 相对时间要跟着走：页面上那一小块显示的是「刚刚 / N 分钟前」，
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

async function runCheck() {
  if (checking.value) return
  dialogOpen.value = true
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

function openDialog() {
  dialogOpen.value = true
}
</script>

<template>
  <!-- ★ 页面上只留这一小块：一个按钮 + 一行结论，不占一整行。 -->
  <div class="selfcheck-inline">
    <el-button size="small" :loading="checking" @click="runCheck">
      <Icon name="refresh" :size="13" style="margin-right: 6px" />启动自检
    </el-button>
    <button v-if="lastCheck" type="button" class="selfcheck-chip" :class="lastCheck.ok ? 'is-ok' : 'is-bad'" @click="openDialog">
      <span class="mark" :class="lastCheck.ok ? 'mark-pass' : 'mark-fail'">{{ lastCheck.ok ? '✓' : '✕' }}</span>
      <span class="chip-text">
        本机上次自检 · {{ agoText }} · {{ summaryText }}
      </span>
      <span class="chip-more">看详情</span>
    </button>
    <span v-else class="selfcheck-hint">尚未自检过 —— 点左边按钮，逐项核对集群与向量。</span>
  </div>

  <!-- 自检过程与结论：都在这个框里 -->
  <el-dialog
    v-model="dialogOpen"
    title="系统自检 · 逐项核对"
    width="600px"
    class="selfcheck-dialog"
    :close-on-click-modal="!checking"
    :show-close="!checking"
  >
    <div class="panel-title-row">
      <span class="panel-subtitle">逐项核对集群与向量一致性</span>
      <span class="live-mark" :class="{ 'is-scan': checking }">
        <i />{{ checking ? 'SCANNING' : lastCheck?.ok === false && !checkSteps.length ? 'IDLE' : 'DONE' }}
      </span>
    </div>

    <div v-if="checkSteps.length" class="check-list">
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
    <p v-else class="text-3" style="font-size: 13px; margin: 0">
      还没有自检记录。点「重新自检」跑一次 —— 十项逐项核对。
    </p>

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

    <template #footer>
      <el-button size="small" :loading="checking" @click="runCheck">重新自检</el-button>
      <el-button size="small" type="primary" :disabled="checking" @click="dialogOpen = false">关闭</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
/* ---- 页面上那一小块 ---- */
.selfcheck-inline {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.selfcheck-chip {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 5px 10px 5px 8px;
  border: 1px solid var(--line);
  border-radius: 999px;
  background: var(--bg-raised);
  color: var(--text-2);
  font-size: 13px;
  font-family: inherit;
  cursor: pointer;
  transition: border-color .2s ease, color .2s ease;
}
.selfcheck-chip:hover { border-color: color-mix(in srgb, var(--accent) 40%, transparent); color: var(--text-1); }
.selfcheck-chip.is-bad { border-color: color-mix(in srgb, var(--danger) 34%, transparent); }
.chip-more { color: var(--accent); margin-left: 2px; }
.selfcheck-hint { color: var(--text-3); font-size: 13px; }

/* ---- 弹框里的逐项扫描 ---- */
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
