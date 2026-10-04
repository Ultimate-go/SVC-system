<script setup>
/**
 * 完整性验证（验证不受限）。
 *
 * - **选文件 + 填第几块**验证（块号支持 0-3, 8 区间语法）+ 允许部分结果开关。
 *   ★ 全局下标是**内部坐标**，只留在"高级"里 —— 主路径上不让它出现。
 * - 展示结论（承诺验证的 code_name）、proof.size_bytes、holders、refs、missing。
 * - 「按指标筛选文件」：所有者、文件标识（包含 / 精确）、块数区间、大小区间、
 *   版本下限、只看我能解密的、只看还没收进集合的……筛出来的是一份**文件清单**
 *   （只 ``GET /api/files``，不取数据、不验证）。
 * - 「集合」：勾中的文件（的每一块）收进一份**清单**，清单存在 localStorage 里，
 *   关掉浏览器再回来还在。集合里每一块都记着"它是谁的第几块"（详情里看）。
 *   点「验证集合里的全部块」才真的去取数据 —— **一次** ``POST /api/query``
 *   把整个集合验一遍，并给出报告。
 * - 「按文件 + 第几块验证」：选文件、填块号（支持 0-3, 8 区间语法）。
 * - 「查登记表」（高级）：全局下标 → 谁的第几块 —— 它本身就是个看内部坐标的工具。
 * - **逐块指纹**：验证结果里可以看到每个下标对应的分量
 *   —— 跟着全局的「简略 / 详细」开关（详细模式才铺出来）。
 */
import { computed, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { evidenceApi } from '../../../api/evidence'
import { filesApi } from '../../../api/files'
import { useAuthStore } from '../../../stores/auth'
import { useBasketStore } from '../../../stores/basket'
import { usePoolStore } from '../../../stores/pool'
import { useThemeStore } from '../../../stores/theme'
import { systemApi } from '../../../api/system'
import { fmtBytes, span } from '../../../utils/format'
// ★ 界面上要的是"第几块"（parseBlockRange）；全局下标只是**高级**里的逃生口
import { parseBlockRange, parseIndexRange } from '../../../utils/validate'
import PageHeader from '../../../components/common/PageHeader.vue'
import DetailToggle from '../../../components/common/DetailToggle.vue'
import VerifyResult from '../../../components/security/VerifyResult.vue'
import BlockFingerprints from '../../../components/security/BlockFingerprints.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'

const pool = usePoolStore()
const theme = useThemeStore()
const basket = useBasketStore()
const auth = useAuthStore()
/** 当前登录名 —— 「只看我的」这类预设要用。 */
const myName = computed(() => auth.username)
/** 详细模式：把逐块分量铺出来（简略模式只给结论）。 */
const detailed = computed(() => theme.detailMode === 'detail')

const indicesInput = ref('0-3, 8')
const allowPartial = ref(false)
const running = ref(false)
const result = ref(null)

/* ---- 验证入口：**选文件 + 第几块**（主交互）----
 *
 * ★ 原来这里要用户填"全局下标"，而全局下标是**内部坐标**（由上传顺序决定）：
 *   想知道"第 2 份文件的第 1 块"是多少，得把前面所有文件占的位置加起来 ——
 *   用户既算不准，错了也看不出来。现在界面上只有文件名与块号，
 *   块号→全局下标 的换算交给后端（``/api/query/files``）。
 *
 * ★ 文件列表**直接用本页已有的 ``allFiles``**（下方过滤/篮子用的就是它），
 *   不再另拉一份 —— 两份列表会出现"这边刚传的文件那边还没有"。
 *
 * 全局下标没有彻底删掉，而是收进"高级"：查登记表、对日志、复现问题时
 * 还是要知道内部坐标的，但不能把它摆在主路径上。
 */
const verifyFileId = ref(null)
const verifyBlocks = ref('0')
const advancedMode = ref(false)

// ---------------------------------------------------------------------------
// 多指标筛选（只列文件：不取数据、不验证）
// ---------------------------------------------------------------------------

//: 筛选项的初值 —— 「重置筛选」就是把它整体盖回去。
const FILTER_DEFAULTS = {
  owner: '',
  keyText: '',
  //: ``contains``（包含）/ ``exact``（精确）
  keyMode: 'contains',
  minBlocks: null,
  maxBlocks: null,
  //: 大小区间单位用 **KB**：让用户填字节既难读也难填
  minKB: null,
  maxKB: null,
  minVersion: null,
  onlyDecryptable: false,
  excludeBasket: false,
  onlyTail: false,
  sortBy: 'id',
}

//: 每一项留空 = 这一项不参与筛选（不填就全都要，这是最不容易出错的默认）。
const filters = reactive({ ...FILTER_DEFAULTS })

const filesLoading = ref(false)
const allFiles = ref([])
const loadError = ref('')
//: 用户勾了哪几个（存文件 id）
const pickedIds = ref([])
//: 最近一次看到的全局块数 —— 用来判"这块还在不在"
const currentN = ref(null)

const owners = computed(() => [...new Set(allFiles.value.map((f) => f.owner))].sort())

/**
 * 给每个文件补两个**派生字段**（后端不提供，前端算）：
 *
 * * ``inBasket`` —— 这份文件已经有多少块在集合里；
 * * ``tailDeletable`` —— 它能不能**整份删掉**（最后一块正好在向量末尾）。
 *   判据是 ``last_index === n-1``：方案只允许从向量末尾往前删，
 *   所以只有排在最后的文件删得动（见后端 files.py 的删除说明）。
 */
const rows = computed(() =>
  allFiles.value.map((f) => {
    const indices = f.indices || []
    return {
      ...f,
      inBasket: basket.countOf(indices),
      tailDeletable: indices.length > 0 && f.last_index === (currentN.value ?? -1),
    }
  }),
)

function hitKeyword(f) {
  const t = filters.keyText.trim()
  if (!t) return true
  return filters.keyMode === 'exact' ? f.file_key === t : f.file_key.includes(t)
}

/** 区间判断：两端都可以只填一边；留空的一边 = 不限。 */
function hitRange(v, lo, hi) {
  if (lo !== null && lo !== '' && Number(v) < Number(lo)) return false
  if (hi !== null && hi !== '' && Number(v) > Number(hi)) return false
  return true
}

/** 筛选 + 排序的结果 —— 界面上那张表就是它。 */
const filtered = computed(() => {
  const out = rows.value.filter((f) => {
    if (filters.owner && f.owner !== filters.owner) return false
    if (!hitKeyword(f)) return false
    if (!hitRange(f.block_count, filters.minBlocks, filters.maxBlocks)) return false
    if (!hitRange((f.total_bytes || 0) / 1024, filters.minKB, filters.maxKB)) return false
    if (
      filters.minVersion !== null &&
      filters.minVersion !== '' &&
      f.version < Number(filters.minVersion)
    ) {
      return false
    }
    if (filters.onlyDecryptable && !f.can_decrypt) return false
    // 「还没收完」= 它的块还没全在集合里（已全在的那种收进去也没用）
    if (filters.excludeBasket && f.inBasket >= (f.indices || []).length) return false
    if (filters.onlyTail && !f.tailDeletable) return false
    return true
  })
  const by = {
    id: (a, b) => a.id - b.id,
    blocks: (a, b) => b.block_count - a.block_count || a.id - b.id,
    bytes: (a, b) => (b.total_bytes || 0) - (a.total_bytes || 0) || a.id - b.id,
    version: (a, b) => b.version - a.version || a.id - b.id,
    basket: (a, b) => b.inBasket - a.inBasket || a.id - b.id,
  }
  return out.sort(by[filters.sortBy] || by.id)
})

const pickedRows = computed(() => filtered.value.filter((f) => pickedIds.value.includes(f.id)))
const pickedBlocks = computed(() =>
  pickedRows.value.reduce((n, f) => n + (f.indices || []).length, 0),
)
const filteredBlocks = computed(() =>
  filtered.value.reduce((n, f) => n + (f.indices || []).length, 0),
)

/** 只把这些列**列出来** —— 不发任何取数据的请求，文件再多也是快的。 */
async function loadFiles() {
  filesLoading.value = true
  loadError.value = ''
  try {
    // 同时问一次"现在一共多少块"：**库空的时候**也得知道，
    // 否则集合里那些已经被删掉的块标不出来（空列表里根本没有 delta_n）。
    const [{ data }, st] = await Promise.all([filesApi.list(), systemApi.status()])
    const list = Array.isArray(data) ? data : data.items || []
    allFiles.value = list

    const n = typeof st?.data?.blocks === 'number' ? st.data.blocks : null
    if (typeof n === 'number') {
      currentN.value = n
      // 让集合知道"现在一共多少块" —— 它要据此标出已经被删掉的下标
      basket.syncN(n)
    }
    if (list.length && list[0].delta_fp) pool.syncDelta({ fp: list[0].delta_fp, n })

    // 刷新后可能少了几份文件：把已经不在的人从勾选里摘掉
    pickedIds.value = pickedIds.value.filter((id) => list.some((f) => f.id === id))
    if (!list.length) loadError.value = '库里还没有任何文件 —— 先到「文件与块」页上传一份'
  } catch (e) {
    loadError.value = e?.response?.data?.detail || e?.message || '加载文件列表失败'
  } finally {
    filesLoading.value = false
  }
}

function togglePick(id) {
  const i = pickedIds.value.indexOf(id)
  if (i >= 0) pickedIds.value.splice(i, 1)
  else pickedIds.value.push(id)
}

function pickAll() {
  pickedIds.value = filtered.value.map((f) => f.id)
}

function clearPicks() {
  pickedIds.value = []
}

function resetFilters() {
  Object.assign(filters, FILTER_DEFAULTS)
}

//: 常用的一键预设 —— 让"多指标"不至于变成"每次都要手填一堆条件"。
function presetMine() {
  resetFilters()
  filters.owner = myName.value
}

function presetAllInBasketTail() {
  resetFilters()
  filters.onlyTail = true
  filters.excludeBasket = true
}

/** 把勾中的文件（每一块）收进集合。 */
function addPickedToBasket() {
  if (!pickedRows.value.length) {
    ElMessage.warning('先勾选要收进集合的文件')
    return
  }
  let added = 0
  for (const f of pickedRows.value) added += basket.addFile(f)
  ElMessage.success(`新收进 ${added} 块 —— 集合现有 ${basket.size} 块`)
}

/** 把当前筛选结果里的文件**全**收进集合（一次收一大批时用）。 */
function addFilteredToBasket() {
  if (!filtered.value.length) {
    ElMessage.warning('当前筛选结果为空')
    return
  }
  let added = 0
  for (const f of filtered.value) added += basket.addFile(f)
  ElMessage.success(`新收进 ${added} 块 —— 集合现有 ${basket.size} 块`)
}

// ---------------------------------------------------------------------------
// 集合（清单）+ 一次性验证
// ---------------------------------------------------------------------------

const basketDetailed = ref(false)
const basketAllowPartial = ref(false)
const basketRunning = ref(false)
const basketResult = ref(null)
const basketError = ref('')

/** 主界面**极简**：只给"多少块、几份文件"和几个下标芯片；详情要展开才铺。 */
// ★ 主界面给的是**界面坐标**（哪份文件的第几块），不再铺内部坐标（全局位置）——
//   这是"选文件 + 第几块"那条主交互的一部分：一串全局位置号用户既看不懂、
//   也没法核对，它是上传顺序的副产物。
const basketGroups = computed(() => basket.groups)
const basketChips = computed(() =>
  basket.groups.slice(0, 8).map((g) => {
    const bs = g.items.map((x) => x.block_idx).filter((b) => typeof b === 'number')
    return {
      key: g.name,
      label:
        g.owner && g.file_key
          ? `${g.file_key}${bs.length ? ` 第 ${span(bs)} 块` : ''}`
          : g.name,
      blocks: g.blocks,
    }
  }),
)
const basketRest = computed(() =>
  Math.max(0, basketGroups.value.length - basketChips.value.length),
)

/**
 * 集合里的每一块现在在哪 —— 从集合自己的来源记录来（不再问登记表）。
 *
 * ★ 这就是"下标为主、同时标出它是谁的第几块"那条要求：主界面只给下标，
 *   这一条给了来源，展开详情才看它。
 *
 * ★ 订正：主界面**不再**铺内部坐标 —— 上面那排芯片已经改成"哪份文件 第几块"，
 *   本条只用在**展开的明细**里（逐块来源）。全局位置是诊断视角的事，
 *   留在「高级」与「查登记表」。
 */
function sourceOf(it) {
  if (!it.owner || !it.file_key) return '来源未知'
  return `${it.owner} / ${it.file_key} 的第 ${it.block_idx} 块`
}

async function verifyBasket() {
  if (!basket.size) {
    ElMessage.warning('集合是空的 —— 先在上面筛一遍，把要验的文件收进集合')
    return
  }
  if (basket.size > 8192) {
    ElMessage.warning(`集合里有 ${basket.size} 块，超过单次上限 8192 —— 请分批验`)
    return
  }
  // ★ 一次验证只能覆盖**同一份文件**：证据是「这份文件的 n」的函数，
  //   跨文件的块聚不成一份证据（后端会明确回 400）。
  //   拦在这里，比让用户提交后看到一句 400 好。
  if (basket.fileCount > 1) {
    ElMessage.warning(
      `集合里现在有 ${basket.fileCount} 份文件 —— 一次验证只能覆盖「同一份文件」。` +
        '请用下面的「移除这组」只留一份，或者按文件分批验。',
    )
    return
  }
  basketRunning.value = true
  basketResult.value = null
  basketError.value = ''
  try {
    // ★ 一次请求把整个集合验完：所有块都在同一条向量里，任意下标子集
    //   本来就能聚成**一份**证据、一次验证（这正是设计 B 的好处）。
    const { data } = await evidenceApi.query(basket.indices, basketAllowPartial.value)
    basketResult.value = data
    basket.syncN(data?.delta_n)
    pool.addCard({
      label: `集合：${basket.size} 块 / ${basket.fileCount} 份文件`,
      src: '完整性验证',
      result: data,
    })
  } catch (e) {
    basketError.value = e?.response?.data?.detail || e?.message || '验证失败'
  } finally {
    basketRunning.value = false
  }
}

function removeFromBasket(i) {
  basket.removeIndex(i)
}

function removeGroupFromBasket(name) {
  basket.removeFile(name)
}

function clearBasket() {
  basket.clear()
  basketResult.value = null
  basketError.value = ''
}

/** 报告里"集合中没被这次结论覆盖"的下标（响应缺的 + 已经被删掉的）。 */
const basketMissed = computed(() => {
  const got = new Set(basketResult.value?.indices || [])
  return basket.indices.filter((i) => !got.has(i))
})

/** 报告里逐块的那张表：下标 → 谁的第几块 → 存在哪台（数据源是响应的 refs）。 */
const basketRefs = computed(() => {
  const holders = basketResult.value?.holders || {}
  return (basketResult.value?.refs || []).map((r) => ({
    ...r,
    holder: holders[r.global_index] ?? '—',
  }))
})

/**
 * 报告里"有几台存储节点参与了这次取块"。
 *
 * ★ 必须**去重**：``holders`` 的键是"哪个下标"、值是"存在哪台" ——
 *   直接数它的键个数得到的是**块数**（5 块就数成 5 台），而节点只有 4 台。
 */
const basketHolderCount = computed(
  () => new Set(Object.values(basketResult.value?.holders || {})).size,
)

const regIndex = ref('')
const regResult = ref(null)
const regRunning = ref(false)

async function runQuery() {
  // 「高级」里填的是全局下标；主路径上填的是"第几块"。
  let indices = null
  let blocks = null
  let picked = null
  if (advancedMode.value) {
    try {
      indices = parseIndexRange(indicesInput.value)
    } catch (e) {
      ElMessage.warning(e.message)
      return
    }
    if (!indices.length) {
      ElMessage.warning('请输入下标')
      return
    }
  } else {
    picked = (allFiles.value || []).find((f) => f.id === verifyFileId.value) || null
    if (!picked) {
      ElMessage.warning('先选一份文件')
      return
    }
    try {
      blocks = parseBlockRange(verifyBlocks.value, picked.block_count)
    } catch (e) {
      ElMessage.warning(e.message)
      return
    }
    if (!blocks.length) {
      ElMessage.warning('请输入第几块')
      return
    }
  }

  running.value = true
  result.value = null
  try {
    // ★ 界面坐标 → 内部坐标的**唯一转换点**：块号交给后端，由它按这份文件的
    //   位置段换（前端不复现那份映射，免得多一处会分叉的真相）。
    const { data } = advancedMode.value
      ? await evidenceApi.query(indices, allowPartial.value)
      : await evidenceApi.queryFiles([[picked.owner, picked.file_key]], blocks)
    result.value = data
    // 顺手把"现在多少块"同步给集合：它据此标出"已经被删掉的下标"
    basket.syncN(data?.delta_n)
    pool.syncDelta({ fp: data?.delta_fp, n: data?.delta_n })
    // 收进证据池（标签用界面坐标写，池子里就不会冒出全局下标）
    pool.addCard({
      label: advancedMode.value
        ? `查询：${indices.join(',')}`
        : `查询：${picked.file_key} 第 ${span(blocks)} 块`,
      src: '完整性验证',
      result: data,
    })
  } catch (e) {
    result.value = { ok: false, verify: { ok: false, code_name: '', message: e?.response?.data?.detail || '查询失败' } }
  } finally {
    running.value = false
  }
}



/**
 * 「当前文件与块数」（按全局下标验证那边）。
 *
 * ★ 一个证据可以横跨多个文件（设计 B），所以这是**真列表**而不是单个值：
 *   逐文件统计这份证据盖住了它几块。数据源是响应里的 refs
 *   （每个全局下标 → 哪个文件的第几块），拿不到归属时如实说“没有归属信息”。
 */
const resultFiles = computed(() => {
  // ★ 优先用响应里的 files：它直接说"哪份文件的第几块"（block_indices），
  //   而这正是界面该说的话。退回 refs 只是为了兼容旧的响应形状。
  const out = []
  for (const f of result.value?.files || []) {
    const blocks = f.block_indices || f.indices || []
    out.push({
      name: `${f.owner} / ${f.file_key}`,
      blocks: blocks.length,
      scope: blocks.length ? `本次覆盖第 ${span(blocks)} 块` : '本次覆盖 0 块',
    })
  }
  if (out.length) return out
  const map = new Map()
  for (const r of result.value?.refs || []) {
    const name = `${r.owner} / ${r.file_key}`
    map.set(name, (map.get(name) || 0) + 1)
  }
  return [...map.entries()].map(([name, blocks]) => ({
    name,
    blocks,
    scope: `本次覆盖 ${blocks} 块`,
  }))
})


async function runRegistry() {
  const i = Number(regIndex.value)
  if (!Number.isInteger(i) || i < 0) {
    ElMessage.warning('请输入非负整数下标')
    return
  }
  regRunning.value = true
  regResult.value = null
  try {
    const { data } = await evidenceApi.registry(i)
    regResult.value = data
  } catch (e) {
    regResult.value = { error: e?.response?.data?.detail || '查不到' }
  } finally {
    regRunning.value = false
  }
}
</script>

<template>
  <div>
    <PageHeader title="完整性验证" subtitle="谁都能验证，登录即可" />

    <div class="panel mb-3">
      <h4 class="sec-title">按文件 + 第几块验证</h4>
      <div class="query-form">
        <template v-if="!advancedMode">
          <el-select v-model="verifyFileId" placeholder="选一份文件" style="width: 240px" filterable>
            <el-option
              v-for="f in allFiles"
              :key="f.id"
              :value="f.id"
              :label="`${f.file_key}（${f.owner}，${f.block_count} 块）`"
            />
          </el-select>
          <el-input
            v-model="verifyBlocks"
            placeholder="第几块，如 0-3, 8"
            style="width: 200px"
            @keyup.enter="runQuery"
          />
        </template>
        <el-input
          v-else
          v-model="indicesInput"
          placeholder="全局下标，如 0-3, 8"
          style="width: 260px"
          @keyup.enter="runQuery"
        />
        <el-switch v-model="allowPartial" active-text="允许部分结果" />
        <el-button type="primary" :loading="running" @click="runQuery">验证</el-button>
        <el-button link type="primary" @click="advancedMode = !advancedMode">
          {{ advancedMode ? '← 改用文件 + 块号' : '高级：直接填全局下标' }}
        </el-button>
      </div>

      <div v-if="result" class="mt-3">
        <VerifyResult :result="result" />
        <!-- 当前文件与块数：一份证据可以横跨多个文件（设计 B），所以逐文件列 -->
        <div class="cur-file">
          <span class="cur-lbl">当前文件与块数</span>
          <template v-if="resultFiles.length">
            <span v-for="f in resultFiles" :key="f.name" class="cur-item mono">
              {{ f.name }}<span class="text-3"> · {{ f.scope }}</span>
            </span>
          </template>
          <span v-else class="cur-item text-3">
            这份证据没带文件归属信息（只有“高级：直接填全局下标”时才会这样）
          </span>
          <span class="cur-total mono">合计 {{ result.indices?.length || 0 }} 块</span>
        </div>
        <div v-if="result.missing?.length" class="missing">
          <el-alert type="warning" :closable="false" :title="`这份结论没覆盖：${result.missing.join(', ')}`" />
        </div>
        <div v-if="result.proof" class="mono text-2 mt-2" style="font-size: 12px">
          证据 {{ result.proof.size_bytes }} 字节（与打开多少块无关）
        </div>
        <!-- 逐块分量：详细模式铺出来，简略模式只说"点详细可看"。 -->
        <div v-if="result.values?.length" class="fp-block">
          <div class="fp-head">
            <span class="text-2" style="font-size: 12px">
              这一份覆盖 {{ result.indices?.length || 0 }} 个下标
            </span>
            <DetailToggle />
          </div>
          <BlockFingerprints
            v-if="detailed"
            :indices="result.indices || []"
            :values="result.values || []"
            :refs="result.refs || []"
          />
          <p v-else class="note">点「详细」逐块看分量指纹（悬浮任意指纹显示完整十六进制）。</p>
        </div>
        <div v-if="result.refs?.length" class="refs mt-2">
          <div v-for="r in result.refs" :key="r.global_index" class="ref-row">
            <span class="mono">{{ r.global_index }}</span>
            <span class="text-2">→ {{ r.owner }} / {{ r.file_key }} 的第 {{ r.block_idx }} 块</span>
            <span class="mono text-3">holder: {{ result.holders?.[r.global_index] }}</span>
          </div>
        </div>
        <StageTimeline v-if="result.timings" :timings="result.timings" class="mt-2" />
      </div>
    </div>

    <!-- ============================================================= -->
    <!-- ① 按指标筛选文件                                                -->
    <!-- ============================================================= -->
    <div class="panel mb-3">
      <div class="sec-head">
        <h4 class="sec-title">按指标筛选文件</h4>
        <el-button link type="primary" size="small" :loading="filesLoading" @click="loadFiles">
          {{ allFiles.length ? '刷新列表' : '拉取文件列表' }}
        </el-button>
      </div>

      <div class="filters">
        <el-select v-model="filters.owner" placeholder="所有者（不限）" clearable style="width: 150px">
          <el-option v-for="o in owners" :key="o" :label="o" :value="o" />
        </el-select>
        <el-select v-model="filters.keyMode" style="width: 100px">
          <el-option label="包含" value="contains" />
          <el-option label="精确" value="exact" />
        </el-select>
        <el-input v-model="filters.keyText" placeholder="文件标识" clearable style="width: 150px" />
        <span class="flt-lbl">块数</span>
        <el-input-number v-model="filters.minBlocks" :min="0" :controls="false" placeholder="少" style="width: 82px" />
        <span class="flt-lbl">-</span>
        <el-input-number v-model="filters.maxBlocks" :min="0" :controls="false" placeholder="多" style="width: 82px" />
        <span class="flt-lbl">大小(KB)</span>
        <el-input-number v-model="filters.minKB" :min="0" :controls="false" placeholder="少" style="width: 82px" />
        <span class="flt-lbl">-</span>
        <el-input-number v-model="filters.maxKB" :min="0" :controls="false" placeholder="多" style="width: 82px" />
        <span class="flt-lbl">版本 ≥</span>
        <el-input-number v-model="filters.minVersion" :min="0" :controls="false" style="width: 76px" />
        <el-select v-model="filters.sortBy" style="width: 124px">
          <el-option label="按编号" value="id" />
          <el-option label="按块数" value="blocks" />
          <el-option label="按大小" value="bytes" />
          <el-option label="按版本" value="version" />
          <el-option label="按已入集合" value="basket" />
        </el-select>
      </div>

      <div class="filters mt-2">
        <el-checkbox v-model="filters.onlyDecryptable">只看我能解密的</el-checkbox>
        <el-checkbox v-model="filters.excludeBasket">只看还没全收进集合的</el-checkbox>
        <el-checkbox v-model="filters.onlyTail">只看能整份删掉的（排在向量末尾）</el-checkbox>
        <el-button link type="primary" size="small" @click="presetMine">只看我的</el-button>
        <el-button link type="primary" size="small" @click="presetAllInBasketTail">末尾且未入集合</el-button>
        <el-button link size="small" @click="resetFilters">重置筛选</el-button>
      </div>

      <p class="note">
        这些条件<strong>全在前端筛</strong>：「拉取文件列表」只调一次 GET /api/files，
        不取证据、不验证，文件再多也只是筛一遍列表。
        下面勾中要收进集合的文件 —— 收进去的是<strong>块</strong>（每个全局下标各算一块），
        收完之后到下面的「集合」里一次验完。
      </p>

      <div v-if="loadError" class="file-error">{{ loadError }}</div>

      <template v-if="allFiles.length">
        <div class="match-head">
          <span class="text-2" style="font-size: 12px">
            共 {{ allFiles.length }} 份，筛出 {{ filtered.length }} 份（{{ filteredBlocks }} 块），
            已勾选 {{ pickedIds.length }} 份（{{ pickedBlocks }} 块）
          </span>
          <el-button link type="primary" size="small" @click="pickAll">全选筛出的</el-button>
          <el-button v-if="pickedIds.length" link type="danger" size="small" @click="clearPicks">清空勾选</el-button>
        </div>

        <div class="match-list">
          <div v-for="f in filtered" :key="f.id" class="file-row">
            <el-checkbox :model-value="pickedIds.includes(f.id)" @change="togglePick(f.id)">
              <span class="mono">{{ f.owner }} / {{ f.file_key }}</span>
            </el-checkbox>
            <span class="mono text-2">{{ f.block_count }} 块</span>
            <span class="mono text-2">{{ fmtBytes(f.total_bytes) }}</span>
            <span class="mono text-3">v{{ f.version }}</span>
            <span class="mono text-3">{{ span(f.indices) }}</span>
            <span v-if="f.can_decrypt" class="tag-ok">可解密</span>
            <span v-if="f.inBasket" class="tag-ok mono">已入集合 {{ f.inBasket }}/{{ f.block_count }}</span>
            <el-button link type="primary" size="small" @click="basket.addFile(f)">收进集合</el-button>
          </div>
        </div>

        <div class="mt-2">
          <el-button type="primary" :disabled="!pickedIds.length" @click="addPickedToBasket">
            把勾选的 {{ pickedIds.length }} 份收进集合（{{ pickedBlocks }} 块）
          </el-button>
          <el-button :disabled="!filtered.length" @click="addFilteredToBasket">
            把筛出的全部收进集合（{{ filteredBlocks }} 块）
          </el-button>
        </div>
      </template>
      <div v-else-if="!filesLoading && !loadError" class="note">
        还没拉取列表 —— 点右上角「拉取文件列表」。
      </div>
    </div>

    <!-- ============================================================= -->
    <!-- ② 集合（清单）：一次把整个集合验完                              -->
    <!-- ============================================================= -->
    <div class="panel mb-3">
      <div class="sec-head">
        <h4 class="sec-title">集合</h4>
        <span class="mono text-2" style="font-size: 12px">
          {{ basket.size }} 块 · {{ basket.fileCount }} 份文件
        </span>
        <el-button v-if="basket.size" link size="small" @click="basketDetailed = !basketDetailed">
          {{ basketDetailed ? '收起详情' : '展开详情' }}
        </el-button>
        <el-button v-if="basket.size" link type="danger" size="small" @click="clearBasket">清空集合</el-button>
      </div>

      <!-- 主界面：极简 —— 只给合计 + 几个下标芯片 -->
      <div v-if="basket.size" class="basket-mini">
        <span v-for="c in basketChips" :key="c.key" class="chip mono">{{ c.label }}</span>
        <span v-if="basketRest" class="chip mono text-3">还有 {{ basketRest }} 份文件…</span>
      </div>
      <div v-else class="note">集合是空的 —— 在上面筛一遍，把要验的文件收进来。</div>

      <el-alert
        v-if="basket.goneIndices.length"
        class="mt-2"
        type="warning"
        :closable="false"
        :title="`集合里有 ${basket.goneIndices.length} 块已经不在向量里了（现在只有 ${basket.n} 块）：${span(basket.goneIndices)}`"
      />

      <!-- 详情：每个下标是谁的第几块（主界面极简，这里才铺出来） -->
      <div v-if="basketDetailed && basket.size" class="basket-detail">
        <div v-for="g in basketGroups" :key="g.name" class="grp">
          <div class="grp-head">
            <span class="mono">{{ g.name }}</span>
            <span class="mono text-3">{{ g.blocks }} 块 · {{ span(g.indices) }}</span>
            <el-button link type="danger" size="small" @click="removeGroupFromBasket(g.name)">移除这组</el-button>
          </div>
          <div class="grp-list">
            <span v-for="it in g.items" :key="it.i" class="item">
              <span class="mono">{{ it.i }}</span>
              <span class="mono text-3">{{ it.block_idx === null ? '第 ? 块' : `第 ${it.block_idx} 块` }}</span>
              <el-button link type="danger" size="small" @click="removeFromBasket(it.i)">×</el-button>
            </span>
          </div>
        </div>
      </div>

      <div class="query-form mt-2">
        <el-button
          type="primary"
          :disabled="!basket.size || basket.fileCount > 1"
          :loading="basketRunning"
          @click="verifyBasket"
        >验证集合里的全部块（{{ basket.size }} 块 · {{ basket.fileCount }} 份文件）</el-button>
        <el-switch v-model="basketAllowPartial" active-text="允许部分结果" />
      </div>
      <p class="note">
        一次请求把整个集合验完：<strong>同一份文件</strong>内的任意下标子集都能聚成
        <strong>一份</strong>证据、一次验证（新架构下一份文件一条向量）。
        所以集合里必须只有一份文件 —— 跨文件的集合请分批验。验完的报告在下面，也会自动进证据池。
      </p>

      <div v-if="basketError" class="file-error">{{ basketError }}</div>

      <div v-if="basketResult" class="mt-3">
        <VerifyResult :result="basketResult" />
        <div class="cur-file">
          <span class="cur-lbl">报告</span>
          <span class="cur-item mono">覆盖 {{ basketResult.indices?.length || 0 }} 块</span>
          <span class="cur-item mono">证据 {{ basketResult.proof?.size_bytes }} 字节</span>
          <span class="cur-item mono">{{ basketHolderCount }} 台节点参与</span>
          <span class="cur-total mono">{{ basket.fileCount }} 份文件</span>
        </div>
        <div v-if="basketMissed.length" class="missing">
          <el-alert
            type="warning"
            :closable="false"
            :title="`这次的结论没覆盖集合里的 ${basketMissed.length} 块：${span(basketMissed)}`"
          />
        </div>
        <div v-if="detailed || basketDetailed" class="refs mt-2">
          <div v-for="r in basketRefs" :key="r.global_index" class="ref-row">
            <span class="mono">{{ r.global_index }}</span>
            <span class="text-2">→ {{ r.owner }} / {{ r.file_key }} 的第 {{ r.block_idx }} 块</span>
            <span class="mono text-3">holder: {{ r.holder }}</span>
          </div>
        </div>
        <BlockFingerprints
          v-if="detailed"
          :indices="basketResult.indices || []"
          :values="basketResult.values || []"
          :refs="basketResult.refs || []"
        />
        <StageTimeline v-if="basketResult.timings" :timings="basketResult.timings" class="mt-2" />
      </div>
    </div>

    <div class="panel">
      <h4 class="sec-title">查登记表（全局下标 → 谁的第几块、存在哪台）</h4>
      <div class="query-form">
        <el-input v-model="regIndex" placeholder="全局下标，如 3" style="width: 160px" />
        <el-button type="primary" :loading="regRunning" @click="runRegistry">查询</el-button>
      </div>
      <div v-if="regResult" class="mt-3">
        <template v-if="!regResult.error">
          <div class="mono text-1" style="font-size: 13px">
            下标 {{ regResult.global_index }} → {{ regResult.owner }} / {{ regResult.file_key }} 的第 {{ regResult.block_idx }} 块
          </div>
          <div class="text-2 mono" style="font-size: 12px">
            holder: {{ regResult.holder }} · replicas: {{ (regResult.replicas || []).join(', ') }}
          </div>
        </template>
        <div v-else class="text-danger">{{ regResult.error }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.sec-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 12px;
}
.query-form {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
.note {
  font-size: 12px;
  color: var(--text-3);
  margin-top: 8px;
}
.file-error {
  margin-top: 10px;
  font-size: 13px;
  color: var(--danger);
}
.match-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 6px;
}
.match-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 320px;
  overflow: auto;
}
.match-row {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
  font-size: 12px;
}
.missing {
  margin-top: 10px;
}
/* 「当前文件与块数」：一横条标签，文件多了自动换行。 */
.cur-file {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 10px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--bg-raised);
  font-size: 12px;
}
.cur-lbl {
  color: var(--accent);
  flex-shrink: 0;
}
.cur-item {
  color: var(--text-1);
}
.cur-total {
  margin-left: auto;
  color: var(--text-3);
}
.fp-block {
  margin-top: 10px;
  border-top: 1px dashed var(--line);
  padding-top: 8px;
}
.fp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}
.refs {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 13px;
}
.ref-row {
  display: flex;
  gap: 10px;
  align-items: baseline;
}
.sec-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.sec-head .sec-title {
  margin-bottom: 0;
}
.filters {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.flt-lbl {
  font-size: 12px;
  color: var(--text-3);
}
.file-row {
  display: flex;
  gap: 10px;
  align-items: center;
  flex-wrap: wrap;
  font-size: 12px;
  padding: 2px 0;
}
.tag-ok {
  color: var(--accent);
  font-size: 12px;
}
/* 集合的极简视图：一行下标芯片，主界面只给这么多 */
.basket-mini {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.chip {
  font-size: 12px;
  padding: 1px 7px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--bg-raised);
  color: var(--text-1);
}
/* 集合的详情视图：每个下标是谁的第几块（展开才铺出来） */
.basket-detail {
  margin-top: 10px;
  border-top: 1px dashed var(--line);
  padding-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.grp-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  font-size: 12px;
}
.grp-list {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-top: 4px;
}
.grp-list .item {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  padding: 1px 6px;
  border: 1px solid var(--line);
  border-radius: 8px;
}
</style>
