<script setup>
/**
 * 可验证查询（验证不受限）。
 *
 * - **选文件 + 填第几块**验证（块号支持 0-3, 8 区间语法）+ 允许部分结果开关。
 *   ★ 全局下标是**内部坐标**，只留在"高级"里 —— 主路径上不让它出现。
 * - 展示结论（承诺验证的 code_name）、proof.size_bytes、holders、refs、missing。
 * - 「按指标筛选文件」：文件持有者、文件标识（包含 / 精确）、块数区间、大小区间，
 *   筛出来的是一份**文件清单**（只 ``GET /api/files``，不取数据、不验证）。
 * - 「集合」：勾中的文件（的每一块）收进一份**清单**，清单存在 localStorage 里，
 *   关掉浏览器再回来还在。集合里每一块都记着"它是谁的第几块"（详情里看）。
 *   点「验证集合里的全部块」才真的去取数据 —— **一次** ``POST /api/query``
 *   把整个集合验一遍，并给出报告。
 * - 「按文件 + 第几块验证」：选文件、填块号（支持 0-3, 8 区间语法）。
 * - **逐块指纹**：验证结果里可以看到每个下标对应的分量
 *   —— 跟着全局的「简略 / 详细」开关（详细模式才铺出来）。
 */
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { evidenceApi } from '../../../api/evidence'
import { filesApi } from '../../../api/files'
import { useBasketStore } from '../../../stores/basket'
import { useAggregateStore } from '../../../stores/aggregate'
import { useThemeStore } from '../../../stores/theme'
import { systemApi } from '../../../api/system'
import { fmtBytes, span, humanList } from '../../../utils/format'
import { MAX_QUERY_INDICES } from '../../../utils/constants'
// ★ 界面上要的是"第几块"（parseBlockRange）；全局下标只是**高级**里的逃生口
import { parseBlockRange, parseIndexRange } from '../../../utils/validate'
import PageHeader from '../../../components/common/PageHeader.vue'
import DetailToggle from '../../../components/common/DetailToggle.vue'
import VerifyResult from '../../../components/security/VerifyResult.vue'
import BlockFingerprints from '../../../components/security/BlockFingerprints.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'
import ProcessDialog from '../../../components/security/ProcessDialog.vue'

const panel = useAggregateStore()
const theme = useThemeStore()
const basket = useBasketStore()
/** 详细模式：把逐块分量铺出来（简略模式只给结论）。 */
const detailed = computed(() => theme.detailMode === 'detail')

const indicesInput = ref('0-3, 8')
const allowPartial = ref(false)
const running = ref(false)
const result = ref(null)
/** 端到端耗时（毫秒）：从点下「验证」到结果返回，含浏览器端重算与网络往返。 */
const verifyTotalMs = ref(0)

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
// ★ 默认填 1（人读从 1 起）：界面上不出现“第 0 块”这种写法。
const verifyBlocks = ref('1')
const advancedMode = ref(false)

// ---------------------------------------------------------------------------
// 多指标筛选（只列文件：不取数据、不验证）
// ---------------------------------------------------------------------------

//: 筛选项的初值（每一项留空 = 这一项不参与筛选）。
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
}

//: 每一项留空 = 这一项不参与筛选（不填就全都要，这是最不容易出错的默认）。
const filters = reactive({ ...FILTER_DEFAULTS })

const filesLoading = ref(false)
const allFiles = ref([])
const loadError = ref('')
//: 用户勾了哪几个（存文件 id）
const pickedIds = ref([])
// ★ 以前这里存的是 /api/status 的 ``blocks``，再用 `i >= n` 判“这块还在不在”。
//   删过**中间**那份文件之后会留下空洞（位置段永不回收），而被删掉的
//   往往**不是**下标最大的那批 —— 那个判据两头都错（审计 F3）。
//   现在改用「存活下标集合」（见 loadFiles 里的 basket.syncLive）。

const owners = computed(() => [...new Set(allFiles.value.map((f) => f.owner))].sort())

/** 拉到的文件**总块数**（拉取框的概览用它）。
 *
 *  ⚠️ 以前这里写的是 `f.n` —— 而 `/api/files` **不返回 `n`**，
 *  于是 `s + undefined` 让整句变成“共 NaN 块”。改与列表页同一口径：`block_count`。 */
const totalBlocks = computed(() => allFiles.value.reduce((s, f) => s + (f.block_count || 0), 0))

//: 拉取框里最多铺多少个文件芯片，剩下的折成一句「还有 N 份…」。
const PULL_CHIP_MAX = 24
const pullChips = computed(() => allFiles.value.slice(0, PULL_CHIP_MAX))
const pullRest = computed(() => Math.max(0, allFiles.value.length - PULL_CHIP_MAX))

/**
 * 给每个文件补一个**派生字段**（后端不提供，前端算）：
 * ``inBasket`` —— 这份文件已经有多少块在集合里。
 */
const rows = computed(() =>
  allFiles.value.map((f) => ({
    ...f,
    inBasket: basket.countOf(f.indices || []),
  })),
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
    return true
  })
  // 固定按登记编号排序（原先的「排序」下拉已移除）。
  return out.sort((a, b) => a.id - b.id)
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
      // 旧判据的傅底（拿不到 /api/files 时用）
      basket.syncN(n)
    }
    // ★ 更准的那一份：**当前还活着哪些下标**（各文件 ``indices`` 的并集）。
    //   集合据此标出“这几块已经被删了” —— 只看总数会被“删中间留下的空洞”
    //   骗到：活着的块被标红、真删的反而标不出来（审计 F3）。
    const live = new Set()
    for (const f of list) for (const i of f.indices || []) live.add(i)
    basket.syncLive([...live])
    if (list.length) panel.syncDelta({ n })

    // 刷新后可能少了几份文件：把已经不在的人从勾选里摘掉
    pickedIds.value = pickedIds.value.filter((id) => list.some((f) => f.id === id))
    if (!list.length) loadError.value = '库里还没有任何文件 —— 先到「上传文件」页上传一份'
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
/** 端到端耗时（毫秒）：集合验证从点击到结果返回。 */
const basketTotalMs = ref(0)

/**
 * 集合一改，上一次的结论就不再是“对这个集合的结论”了 —— 直接把它收起来。
 *
 * ★ 以前留着它、再在上面贴一句“这次的结论没覆盖集合里的 N 块”，结果就是
 *   一份旧报告配一条自己解释自己的警告，反而要多看两眼才能明白“已经作废了”。
 * ★ 信号用 ``basket.indices.join(',')``（一个字符串）：集合增减/替换都会
 *   改变它，而它又不会因为无关的渲染而变化。
 */
watch(
  () => basket.indices.join(','),
  () => {
    basketResult.value = null
    basketError.value = ''
  },
)

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
  if (basket.size > MAX_QUERY_INDICES) {
    ElMessage.warning(
      `集合里有 ${basket.size} 块，超过单次上限 ${MAX_QUERY_INDICES} —— 请分批验`,
    )
    return
  }
  // ★ 集合**可以跨文件**：``POST /api/query`` 接的就是全库下标，遇到多份文件时
  //   它会在「合并位置集」上重算一份证据（秒级），响应里逐文件回 ``files[]``。
  //   （早先这里拦了 ``fileCount > 1`` —— 那是把 ``verify_batch`` / ``disagg``
  //    的限制错安到了这条路径上，结果把「多块 / 跨文件先收进集合再验」堵死了。）
  const t0 = performance.now()
  basketRunning.value = true
  basketResult.value = null
  basketError.value = ''
  try {
    // ★ 一次请求把整个集合验完：集合里可能跨了多份文件，后端会在
    //   合并位置集上重算一份证据，所以这里仍然只发一次请求、只验一次。
    const { data } = await evidenceApi.query(basket.indices, basketAllowPartial.value)
    basketResult.value = data
    basket.syncN(data?.delta_n)
    panel.addCard({
      label: `集合：${basket.size} 块 / ${basket.fileCount} 份文件`,
      src: '可验证查询',
      result: data,
    })
  } catch (e) {
    basketError.value = e?.response?.data?.detail || e?.message || '验证失败'
  } finally {
    basketTotalMs.value = Math.round(performance.now() - t0)
    basketRunning.value = false
  }
}

/**
 * 把一份文件里**指定的几块**收进集合（界面坐标 → 全局下标在这一处换）。
 *
 * ``file.indices[b]`` 就是第 ``b`` 块的全局下标（登记表按块号连续分配，
 * 所以 ``indices`` 的位次就是块号）—— 与 ``addFile`` 用的是同一份映射。
 */
function addBlocksOf(file, blocks) {
  const map = file?.indices || []
  let added = 0
  for (const b of blocks) {
    const i = map[b]
    if (i === undefined) continue
    added += basket.addIndices([i], {
      owner: file.owner,
      file_key: file.file_key,
      block_idx: b,
    })
  }
  return added
}

/**
 * 把「按文件 + 第几块」那一格里选中的块**收进集合**（只攒着，不验证）。
 *
 * 这是“少量块”与“多块 / 跨文件”两条路之间的桥：先攒，最后在集合里一次验完。
 */
function addToBasket() {
  if (advancedMode.value) {
    let idx = []
    try {
      idx = parseIndexRange(indicesInput.value)
    } catch (e) {
      ElMessage.warning(e.message)
      return
    }
    if (!idx.length) {
      ElMessage.warning('请输入下标')
      return
    }
    const added = basket.addIndices(idx)
    ElMessage.success(
      added ? `收进集合 ${added} 块（手上是全局下标，不标文件归属）` : '这些块已经在集合里了',
    )
    return
  }
  const picked = (allFiles.value || []).find((f) => f.id === verifyFileId.value) || null
  if (!picked) {
    ElMessage.warning('先选一份文件')
    return
  }
  let blocks = []
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
  const added = addBlocksOf(picked, blocks)
  ElMessage.success(
    added
      ? `收进集合 ${added} 块（${picked.file_key} 的 ${span(blocks)} 块）—— 集合现有 ${basket.size} 块`
      : '这些块已经在集合里了',
  )
}

/**
 * 文件列表里的「选择收录」：就地弹一个输入框，只把这几块收进集合。
 *
 * 不另开弹窗组件 —— 要填的东西就一个“第几块”，用 Element Plus 自带的
 * ``ElMessageBox.prompt`` 最省事，也不会再多一块需要维护的 UI。
 */
async function addBlocksPrompt(f) {
  let value = ''
  try {
    const r = await ElMessageBox.prompt(
      `这份文件共 ${f.block_count} 块。填第几块（如 0-3, 8），只把这几块收进集合。`,
      `选择收录：${f.owner} / ${f.file_key}`,
      {
        confirmButtonText: '收进集合',
        cancelButtonText: '取消',
        inputPlaceholder: '第几块，如 0-3, 8',
        inputValidator: (v) => {
          try {
            return parseBlockRange(v, f.block_count).length ? true : '请输入第几块'
          } catch (e) {
            return e.message
          }
        },
      },
    )
    value = r.value
  } catch (e) {
    return // 取消 / 关闭
  }
  const blocks = parseBlockRange(value, f.block_count)
  const added = addBlocksOf(f, blocks)
  ElMessage.success(
    added
      ? `收进集合 ${added} 块（${f.file_key} 的 ${span(blocks)} 块）—— 集合现有 ${basket.size} 块`
      : '这些块已经在集合里了',
  )
}

/** 跨文件时，把这份证据覆盖到哪几份文件列出来（响应里的 ``files[]``）。 */
const basketFiles = computed(() =>
  (basketResult.value?.files || []).map((f) => ({
    name: `${f.owner} / ${f.file_key}`,
    scope: `${f.n} 块`,
  })),
)

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

/**
 * 集合里的全局下标 → 「哪份文件的第几块」。
 *
 * ★★ 为什么需要它：全局下标是**内部坐标**（上传顺序的副产物），界面上只说
 *   “哪份文件的第几块”（用户反馈：分解/集合这些地方还在印 3159、3160 这种数）。
 *   好在集合项自己就带归属（`addFile` / `addResult` 收进来时记下的
 *   `owner / file_key / block_idx`），所以这里是现成的，不用额外请求。
 *
 * 做一次 Map 是因为 :func:`basketText` 要按几十上百个下标查它（别 O(n²)）。
 */
const basketItemMap = computed(() => {
  const m = new Map()
  for (const it of basket.items) m.set(it.i, it)
  return m
})

/** 一组全局下标 → 界面坐标（按文件归并，如「病历A 第 0-3 块（4 块）」）。 */
function basketText(list) {
  const gs = Array.isArray(list) ? list : []
  if (!gs.length) return '（空）'
  const byFile = new Map()
  let unknown = 0
  for (const i of gs) {
    const it = basketItemMap.value.get(Number(i))
    if (!it || it.block_idx === null || it.block_idx === undefined) {
      unknown += 1
      continue
    }
    const name = it.file_key || '?'
    if (!byFile.has(name)) byFile.set(name, [])
    byFile.get(name).push(it.block_idx)
  }
  const parts = [...byFile.entries()].map(
    ([name, bs]) => `${name} 第 ${span(bs)} 块（${bs.length} 块）`,
  )
  // ★ 老项（从“按全局下标”那条路收进来时才可能没有归属）如实说，不编块号。
  if (unknown) parts.push(`${unknown} 块没有归属信息`)
  return parts.join('；')
}

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

/* ---------------------------------------------------------------------------
   验证过程弹框（★ 2026-10-12 用户要的）
   ---------------------------------------------------------------------------
   检验完全由公开量完成：δ、π_I 与本地重算的分量即可判定，无需采信服务端的结论。
   弹框逐步展示这些量是怎样产生的，以及每一步在链路上的作用。
   `key` 与后端 `with stage("…")` 的名字对齐；对不上就不显示耗时，不编数字。
--------------------------------------------------------------------------- */
const VERIFY_STEPS = [
  {
    name: '确定下标集合',
    en: 'SELECT',
    desc: '仅对指定分块作结论。一份证据在其覆盖的下标上有定义，未被覆盖的分块不因这份证据而被判定为通过。',
  },
  {
    key: '探测节点持有情况',
    // ★ 只改给人的名字，``key`` 不动 —— 它与后端 ``with stage("…")`` 的名字对齐
    //   （scripts/check_stage_names.py 钉的就是 key），改了名字就没耗时了。
    name: '查询节点持有情况',
    en: 'PROBE',
    desc: '查询各存储节点，集合内的分块当前分别由谁持有。节点的申报只作为参考，随后要由公开检验加以核对，本身不构成结论。',
  },
  {
    key: '取回密文',
    name: '取回密文',
    en: 'FETCH',
    desc: '从存储节点取回选定分块的密文。',
  },
  {
    key: '取回分量与凭证',
    name: '重新计算分量',
    en: 'DIGEST',
    desc: '浏览器在本地对取回的密文重新计算公开分量（即密文的 SM3 摘要），不采用服务端提供的数值。公开分量由密文唯一确定。',
  },
  {
    key: '证据合成',
    name: '证据聚合',
    en: 'AGGREGATE',
    desc: '把各分块的公开分量按身份聚合为一份证据：规模恒为两个群元素、不超过 256 字节，与本次覆盖的分块数无关。',
  },
  {
    key: '承诺验证',
    name: '公开检验',
    en: 'VERIFY',
    desc: '将聚合承诺 δ = (U, C) 与所得证据执行一次配对检验。该检验只用到公开量：不需明文、不需任何密钥，也不需要信任任何一方，任何持有同样输入的主体都能独立复算。',
  },
  {
    name: '结果判定',
    en: 'VERDICT',
    // 实际文案由 verifySteps 按「这次到底通没通」填一行（见下）。
    desc: '',
  },
]

/** 结果判定那一步的两句话：只显示**一句**，取决于本次结论。 */
const PASS_LINE = '可验证查询结果通过：分块存在，且内容与入库版本一致。'
const FAIL_LINE =
  '可验证查询结果不通过：存在被替换或被删除的分块，或返回的密文与当前承诺不符 —— 两种情况都需要重新取证核实。'

const processOpen = ref(false)
const lastVerify = ref(null)

/**
 * 本次验证的范围：「哪一份文件、哪些块」。
 *
 * ★ 它只出现在「确定下标集合」那一步的正文里（用户要求把原来右上角那排
 *   中间量「范围 / δ_n / δ 指纹」整个去掉，范围信息并入正文）。
 */
const lastVerifyScope = ref({ file: '', blocks: '' })

const verifySteps = computed(() => {
  const sc = lastVerifyScope.value
  const passed = lastVerify.value?.ok !== false
  return VERIFY_STEPS.map((s) => {
    if (s.en === 'SELECT') {
      // 换行靠 ProcessDialog 里 `.desc` 的 `white-space: pre-line`（见那边注释）。
      return { ...s, desc: `${s.desc}\n当前选择文件 ${sc.file}\n选定位置 ${sc.blocks}` }
    }
    if (s.en === 'VERDICT') return { ...s, desc: passed ? PASS_LINE : FAIL_LINE }
    return s
  })
})

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

  const t0 = performance.now()
  running.value = true
  result.value = null
  try {
    // ★ 界面坐标 → 内部坐标的**唯一转换点**：块号交给后端，由它按这份文件的
    //   位置段换（前端不复现那份映射，免得多一处会分叉的真相）。
    const { data } = advancedMode.value
      ? await evidenceApi.query(indices, allowPartial.value)
      : await evidenceApi.queryFiles([[picked.owner, picked.file_key]], blocks)
    result.value = data
    // ★ 验证刚跑完时把过程弹出来：结论谁都会给，"凭什么说这个结论"才是这套系统的东西。
    lastVerify.value = data
    lastVerifyScope.value = advancedMode.value
      ? { file: '（高级：按全局下标，不针对某一份文件）', blocks: `全局下标 ${span(indices)}` }
      : { file: picked.file_key, blocks: `第 ${span(blocks)} 块` }
    processOpen.value = true
    // 顺手把"现在多少块"同步给集合：它据此标出"已经被删掉的下标"
    basket.syncN(data?.delta_n)
    panel.syncDelta({ n: data?.delta_n })
    // 入池（标签用界面坐标写，聚合面板里就不会冒出全局下标）
    panel.addCard({
      label: advancedMode.value
        ? `查询：${indices.join(',')}`
        : `查询：${picked.file_key} 第 ${span(blocks)} 块`,
      src: '可验证查询',
      result: data,
    })
  } catch (e) {
    result.value = { ok: false, verify: { ok: false, code_name: '', message: e?.response?.data?.detail || '查询失败' } }
  } finally {
    verifyTotalMs.value = Math.round(performance.now() - t0)
    running.value = false
  }
}



/**
 * 「当前文件与块数」（按全局下标验证那边）。
 *
 * ★ 一个证据可以横跨多个文件（跨文件时在合并位置集上重算），所以这是**真列表**而不是单个值：
 *   逐文件统计这份证据盖住了它几块。数据源是响应里的 refs
 *   （每个全局下标 → 哪个文件的第几块），拿不到归属时如实说“没有归属信息”。
 */
const resultFiles = computed(() => {
  // ★★ 数据源是响应里的 **refs**（每个全局下标 → 哪个文件的第几块）——
  //   它是**齐全**的。
  //
  //   以前这里优先读 ``files[].block_indices``，而 ``/api/query`` 的 ``files[]``
  //   **根本没有**这个字段（只有 ``offset / n / delta_fp`` 那份账目摘要 ——
  //   带 ``block_indices`` 的是 ``/api/query/files``）。于是每一份都显示
  //   “本次覆盖 0 块”；又因为“每份文件都 push 了一行”，下面那段本来正确的
  //   ``refs`` 回退分支**永远跑不到**（审计 F2，实测复现）。
  const refs = result.value?.refs || []
  if (refs.length) {
    const byFile = new Map()
    for (const r of refs) {
      const id = `${r.owner} / ${r.file_key}`
      const e = byFile.get(id) || { id, name: r.file_key, blocks: [] }
      e.blocks.push(Number(r.block_idx))
      byFile.set(id, e)
    }
    return [...byFile.values()].map((e) => ({
      // ★ 界面只显示**文件标识**（用户口径：不把“谁的文件”拼在前面）。
      //   键单独留一份 `owner / file_key`：不同拥有者可能有同名标识，
      //   直接拿标识当 key 会撞。
      id: e.id,
      name: e.name,
      blocks: e.blocks.length,
      scope: `本次覆盖第 ${span(e.blocks)} 块`,
    }))
  }
  // 没有 refs（比如空结果）时退回响应里的账目摘要：用**实际问到的下标**
  // 与本文件的区间 ``[offset, offset+n)`` 求交 —— 宁可算出 0，也不能
  // 因为“字段不存在”就把每一份都写成 0。
  // ★★ 求交之后再减去 `offset`（审计 N7）：截出来的是**全局下标**，
  //   而上面 refs 分支用的是 `block_idx`（文件内块号）。两套坐标混在同一处，
  //   一旦 refs 变成空的就会打出一串“第 3159 块”。
  const asked = result.value?.indices || []
  return (result.value?.files || []).map((f) => {
    const off = Number(f.offset) || 0
    const hit = asked.filter((i) => i >= off && i < off + Number(f.n)).map((i) => i - off)
    return {
      id: `${f.owner} / ${f.file_key}`,
      name: f.file_key,
      blocks: hit.length,
      scope: hit.length ? `本次覆盖第 ${span(hit)} 块` : '本次覆盖 0 块',
    }
  })
})


// 进入页面即自动拉取文件列表（列表是所有后续操作的前提）。
onMounted(loadFiles)
</script>

<template>
  <div class="verify-page">
    <PageHeader title="可验证查询" subtitle="验证公开；解密需所有权" />

    <!-- ============================================================= -->
    <!-- ⓪ 拉取文件列表（单独一块，放最上面）                            -->
    <!-- ============================================================= -->
    <!-- ★ 为什么单独拿出来：它原来是「按指标筛选文件」右上角一个小小的链接按钮，
         于是“我还没拉列表”这件事要在页面**下半部分**才看得出来 —— 而它其实是
         后面所有版块的**前提**（没列表就没得筛、没得勾）。
         现在放到最上面、独立成块，并把“当前拉到几份”直接写出来。 -->
    <div class="panel mb-3">
      <div class="sec-head">
        <h4 class="sec-title">拉取文件列表</h4>
        <el-button type="primary" size="small" :loading="filesLoading" @click="loadFiles">
          重新拉取
        </el-button>
      </div>
      <!-- ★ 拉取结果**就地**展示在这个框里：概览 + 文件清单。
           以前这里只有一句“已拉取 N 份”，清单在下面「按指标筛选」那一块 ——
           于是“我拉到了什么”和“我在哪拉的”被拆到两处。 -->
      <div v-if="loadError" class="file-error">{{ loadError }}</div>

      <template v-else-if="allFiles.length">
        <div class="pull-summary mono text-2" style="font-size: 13px">
          已拉取 {{ allFiles.length }} 份文件 · 共 {{ totalBlocks }} 块 · {{ owners.length }} 位所有者
        </div>
        <!-- ★ 一行只留**文件标识**（带一个小方框）：文件多了也不会变成一堆
             “owner/名字 · n 块”的长文本；其余信息悬浮看，纵列对齐、一眼扫完。 -->
        <div class="pull-files">
          <el-tooltip
            v-for="f in pullChips"
            :key="f.id"
            placement="top"
            :show-after="120"
            popper-class="vds-chip-tip"
          >
            <template #content>
              <div class="tip-rows">
                <div class="tip-row"><span>所有者</span><b>{{ f.owner }}</b></div>
                <div class="tip-row"><span>文件标识</span><b>{{ f.file_key }}</b></div>
                <div class="tip-row"><span>大小</span><b>{{ fmtBytes(f.total_bytes) }}</b></div>
                <div class="tip-row"><span>分块</span><b>{{ f.block_count }} 块 · 每块 {{ f.segment_bytes }} B</b></div>
                <div class="tip-row"><span>版本</span><b>v{{ f.version }}</b></div>
                <div class="tip-row"><span>创建</span><b>{{ (f.created_at || '').replace('T', ' ') }}</b></div>
              </div>
            </template>
            <span class="pull-chip mono">{{ f.file_key }}</span>
          </el-tooltip>
          <span v-if="pullRest" class="pull-chip is-more mono">还有 {{ pullRest }} 份…</span>
        </div>
     
      </template>

      <div v-else-if="filesLoading" class="text-3" style="font-size: 13px">正在拉取…</div>
      <div v-else class="text-3" style="font-size: 13px">列表为空。</div>
    </div>

    <!-- ============================================================= -->
    <!-- ① 按文件 + 第几块验证（快速验证）                                -->
    <!-- ============================================================= -->
    <div class="panel mb-3">
      <div class="sec-head">
        <h4 class="sec-title">按文件 + 第几块验证</h4>
        <span class="step-tag">快速验证</span>
      </div>
      <div class="query-form">
        <template v-if="!advancedMode">
          <!-- ★ 下拉里只显示**文件标识**（与上面拉取列表的芯片同一口径）：
               “名字（所有者，N 块）”在窄下拉里偺被截断，反而更看不清。 -->
          <el-select v-model="verifyFileId" placeholder="选择文件标识" style="width: 240px" filterable>
            <el-option
              v-for="f in allFiles"
              :key="f.id"
              :value="f.id"
              :label="f.file_key"
            />
          </el-select>
          <el-input
            v-model="verifyBlocks"
            placeholder="第几块，如 1-4, 9"
            style="width: 200px"
            @keyup.enter="runQuery"
          />
        </template>
        <el-input
          v-else
          v-model="indicesInput"
          placeholder="全局编号，如 1-4, 9"
          style="width: 260px"
          @keyup.enter="runQuery"
        />
        <el-switch v-model="allowPartial" active-text="允许部分结果" />
        <el-button type="primary" :loading="running" @click="runQuery">验证</el-button>
        <el-button :disabled="advancedMode ? false : !verifyFileId" @click="addToBasket">
          加入集合
        </el-button>
        <el-button link type="primary" @click="advancedMode = !advancedMode">
          {{ advancedMode ? '← 改用文件 + 块号' : '高级：直接填全局下标' }}
        </el-button>
      </div>

      <div v-if="result" class="mt-3">
        <!-- ★ 只留一句标题结论（“验证通过” / “验证失败 · 环节”）。
             下面那行 detail 在后端成功时就是又一句“验证通过”，与标题完全重复 ——
             用户要求去掉；失败时那句才是有用的（“哪一步没过”），仍然显示。 -->
        <VerifyResult :result="result" :show-detail="result.verify?.ok === false" />
        <!-- 当前文件与块数：一份证据可以横跨多个文件（跨文件时重算），所以逐文件列 -->
        <div class="cur-file">
          <span class="cur-lbl">当前文件与块数</span>
          <template v-if="resultFiles.length">
            <span v-for="f in resultFiles" :key="f.id" class="cur-item mono">
              {{ f.name }}<span class="text-3"> · {{ f.scope }}</span>
            </span>
          </template>
          <span v-else class="cur-item text-3">
            这份证据没带文件归属信息（只有“高级：直接填全局下标”时才会这样）
          </span>
          <span class="cur-total mono">合计 {{ result.indices?.length || 0 }} 块</span>
        </div>
        <div v-if="result.missing?.length" class="missing">
          <el-alert type="warning" :closable="false" :title="`这份结论没覆盖：${humanList(result.missing).join(', ')}`" />
        </div>
        <!-- 逐块明细：分量指纹 + 归属**合在同一个折叠里**。
             块数一多，两块各自铺开就会把页面撑得很长；合成一个折叠后“想收就都收”。
             ★ 默认**收起**（用户口径）：查询页的重点是结论，明细想看看再点开。 -->
        <details
          v-if="result.values?.length || result.refs?.length"
          class="refs-collapse"
        >
          <summary>
            <span class="tri" aria-hidden="true">▶</span>
            逐块明细（{{ result.indices?.length || 0 }} 块）
            <span class="sum-note">分量指纹与当前持有节点</span>
          </summary>
          <div class="fp-head mt-2">
            <span class="text-2" style="font-size: 13px">每块的公开分量</span>
            <DetailToggle />
          </div>
          <BlockFingerprints
            v-if="detailed"
            :indices="result.indices || []"
            :values="result.values || []"
            :refs="result.refs || []"
            :show-source="false"
          />
          <p v-else class="note">点「详细」逐块看分量指纹（悬浮任意指纹显示完整十六进制）。</p>
          <div v-if="result.refs?.length" class="refs mt-3">
            <div v-for="r in result.refs" :key="r.global_index" class="ref-row">
              <span class="mono">第 {{ r.block_idx }} 块</span>
              <span class="text-2">→ {{ r.owner }} / {{ r.file_key }}</span>
              <span class="mono text-3">holder: {{ result.holders?.[r.global_index] }}</span>
            </div>
          </div>
        </details>
        <StageTimeline
          v-if="result.timings"
          :timings="result.timings"
          :total-ms="verifyTotalMs"
          :state="result.verify?.ok === false ? 'fail' : 'ok'"
          :default-open="false"
          class="mt-2"
        />
      </div>

      <details class="note-collapse">
        <summary>说明</summary>
        <p class="note">
          选一份文件、填第几块，点「验证」即可查看结果。要验证多块或跨文件时，
          点「加入集合」把块收进集合，再到下方「集合」统一验证。
        </p>
      </details>
    </div>

    <!-- ============================================================= -->
    <!-- ② 筛选文件 + 集合：一左一右两栏（窄屏自动折成一列）              -->
    <!-- ============================================================= -->
    <div class="two-col">
    <div class="panel mb-3">
      <div class="sec-head">
        <h4 class="sec-title">按指标筛选文件</h4>
        <span class="step-tag">选块入集合</span>
      </div>

      <!-- ★ 每个指标一组：`.flt` 里左边是定宽右对齐的标签、右边是控件。
           用网格排（见 <style> 里的 `.filters`），所以所有标签与控件都对到同一条竖线。 -->
      <div class="filters">
        <div class="flt">
          <span class="flt-lbl">文件持有者</span>
          <el-select v-model="filters.owner" placeholder="不限" clearable style="width: 100%">
            <el-option v-for="o in owners" :key="o" :label="o" :value="o" />
          </el-select>
        </div>

        <div class="flt">
          <span class="flt-lbl">文件标识</span>
          <el-select v-model="filters.keyMode" style="width: 84px">
            <el-option label="包含" value="contains" />
            <el-option label="精确" value="exact" />
          </el-select>
          <el-input
            v-model="filters.keyText"
            placeholder="关键字"
            clearable
            style="flex: 1 1 0; min-width: 0"
          />
        </div>

        <div class="flt">
          <span class="flt-lbl">块数</span>
          <div class="flt-range">
            <el-input-number v-model="filters.minBlocks" :min="0" :controls="false" placeholder="下限" />
            <span class="text-3">—</span>
            <el-input-number v-model="filters.maxBlocks" :min="0" :controls="false" placeholder="上限" />
          </div>
        </div>

        <div class="flt">
          <span class="flt-lbl">大小/KB</span>
          <div class="flt-range">
            <el-input-number v-model="filters.minKB" :min="0" :controls="false" placeholder="下限" />
            <span class="text-3">—</span>
            <el-input-number v-model="filters.maxKB" :min="0" :controls="false" placeholder="上限" />
          </div>
        </div>
      </div>

      <template v-if="allFiles.length">
        <div class="match-head">
          <span class="text-2" style="font-size: 13px">
            共 {{ allFiles.length }} 份，筛出 {{ filtered.length }} 份（{{ filteredBlocks }} 块），
            已勾选 {{ pickedIds.length }} 份（{{ pickedBlocks }} 块）
          </span>
          <!-- ★ 这个按钮只作用于**筛出来的那些**（pickAll 用的是 filtered），
               所以叫“全选筛出的”而不是笼统的“全选”。 -->
          <el-button link type="primary" size="small" @click="pickAll">全选筛出的</el-button>
          <el-button v-if="pickedIds.length" link type="danger" size="small" @click="clearPicks">清空勾选</el-button>
        </div>

        <div class="match-list">
          <div v-for="f in filtered" :key="f.id" class="file-row">
            <el-checkbox :model-value="pickedIds.includes(f.id)" @change="togglePick(f.id)" />
            <span class="mono f-owner" :title="f.owner">（{{ f.owner }}）</span>
            <span class="mono f-name" :title="f.file_key">{{ f.file_key }}</span>
            <span class="mono text-2">{{ f.block_count }} 块</span>
            <span class="mono text-2">{{ fmtBytes(f.total_bytes) }}</span>
            <span class="mono text-3">v{{ f.version }}</span>
            <span class="mono tag-ok">已入集合 {{ f.inBasket }}/{{ f.block_count }}</span>
            <el-button link type="primary" size="small" @click="basket.addFile(f)">完整收录</el-button>
            <el-button link type="primary" size="small" @click="addBlocksPrompt(f)">选择收录</el-button>
          </div>
        </div>

        <div class="mt-2">
          <el-button type="primary" :disabled="!pickedIds.length" @click="addPickedToBasket">
            把勾选的文件收进集合
          </el-button>
        </div>
      </template>
      <div v-else-if="!filesLoading && !loadError" class="note">
        列表未加载。请执行「重新拉取」。
      </div>

      <details class="note-collapse">
        <summary>说明</summary>
        <p class="note">
          用上面的条件筛选文件，勾选后点「把勾选的…收进集合」，或点每行的「完整收录」「选择收录」
          把文件收进集合。收集完成后到下方「集合」统一验证。
        </p>
      </details>
    </div>

    <!-- ============================================================= -->
    <!-- ③ 集合（清单）：一次把整个集合验完                              -->
    <!-- ============================================================= -->
    <div class="panel mb-3">
      <div class="sec-head">
        <h4 class="sec-title">集合</h4>
        <span class="step-tag">统一验证</span>
        <span class="mono text-2" style="font-size: 13px">
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
        :title="`集合里有 ${basket.goneIndices.length} 块已经不在库里了 —— ${basketText(basket.goneIndices)}`"
      />

      <!-- 详情：每个下标是谁的第几块（主界面极简，这里才铺出来） -->
      <div v-if="basketDetailed && basket.size" class="basket-detail">
        <div v-for="g in basketGroups" :key="g.name" class="grp">
          <div class="grp-head">
            <span class="mono">{{ g.name }}</span>
            <span class="mono text-3">{{ g.blocks }} 块 · {{ basketText(g.indices) }}</span>
            <el-button link type="danger" size="small" @click="removeGroupFromBasket(g.name)">移除这组</el-button>
          </div>
          <div class="grp-list">
            <span v-for="it in g.items" :key="it.i" class="item">
              <span class="mono">{{ it.block_idx === null || it.block_idx === undefined ? '第 ? 块' : `第 ${it.block_idx} 块` }}</span>
              <el-button link type="danger" size="small" @click="removeFromBasket(it.i)">×</el-button>
            </span>
          </div>
        </div>
      </div>

      <div class="query-form mt-2">
        <el-button
          type="primary"
          :disabled="!basket.size"
          :loading="basketRunning"
          @click="verifyBasket"
        >验证集合里的全部块（{{ basket.size }} 块 · {{ basket.fileCount }} 份文件）</el-button>
        <el-switch v-model="basketAllowPartial" active-text="允许部分结果" />
      </div>

      <div v-if="basketError" class="file-error">{{ basketError }}</div>

      <details class="note-collapse">
        <summary>说明</summary>
        <p class="note">
          把要验证的块收进集合后，点「验证集合里的全部块」，一次验完集合里的所有块，结果显示在下方。
        </p>
      </details>
    </div>
    </div>

    <!-- ============================================================= -->
    <!-- ③ 集合验证结果：单独占一行（结论 + 报告 + 逐块归属 + 耗时）      -->
    <!-- ============================================================= -->
    <div v-if="basketResult" class="panel mb-3">
      <div class="sec-head">
        <h4 class="sec-title">集合验证结果</h4>
        <span class="step-tag">一次结论</span>
      </div>
        <VerifyResult :result="basketResult" />
        <!-- 一句总述代替原来那排“报告 / 覆盖 / 证据字节 / 参与节点 / 份数”：
             用户要的是“这次验了多少东西”，不是内部统计项。 -->
        <div class="cur-file">
          <span class="cur-lbl">本次验证覆盖</span>
          <span class="cur-item mono">{{ basketFiles.length || basket.fileCount }} 个文件共 {{ basketResult.indices?.length || 0 }} 块</span>
          <span class="cur-item mono">{{ basketHolderCount }} 台存储节点</span>
        </div>
        <div v-if="basketFiles.length > 1" class="cur-file">
          <span class="cur-lbl">这份证据覆盖</span>
          <span v-for="f in basketFiles" :key="f.name" class="cur-item mono">
            {{ f.name }}<span class="text-3"> · {{ f.scope }}</span>
          </span>
        </div>
        <details v-if="basketRefs.length" class="refs-collapse">
          <summary>
            <span class="tri" aria-hidden="true">▶</span>
            逐块明细（{{ basketRefs.length }} 块）
            <span class="sum-note">分量指纹与当前持有节点</span>
          </summary>
          <div v-if="detailed" class="mt-2">
            <BlockFingerprints
              :indices="basketResult.indices || []"
              :values="basketResult.values || []"
              :refs="basketResult.refs || []"
              :show-source="false"
            />
          </div>
          <p v-else class="note">点「详细」逐块看分量指纹（悬浮任意指纹显示完整十六进制）。</p>
          <div class="refs mt-3">
            <div v-for="r in basketRefs" :key="r.global_index" class="ref-row">
              <span class="mono">第 {{ r.block_idx }} 块</span>
              <span class="text-2">→ {{ r.owner }} / {{ r.file_key }}</span>
              <span class="mono text-3">holder: {{ r.holder }}</span>
            </div>
          </div>
        </details>
        <StageTimeline
          v-if="basketResult.timings"
          :timings="basketResult.timings"
          :total-ms="basketTotalMs"
          :default-open="false"
          class="mt-2"
        />
    </div>

    <!-- 验证过程：逐步讲清“这个结论是怎么得到的”（耗时对齐真实阶段名）。 -->
    <ProcessDialog
      v-model="processOpen"
      title="一次可验证查询的处理链路"

      :steps="verifySteps"
      :timings="lastVerify?.timings"
      :e2e-ms="verifyTotalMs"
    />
  </div>
</template>

<style scoped>
.sec-title {
  font-size: 17px;
  font-weight: 600;
  color: var(--text-1);
  margin-bottom: 12px;
}
.query-form {
  display: flex;
  gap: 12px;
  align-items: center;
  flex-wrap: wrap;
}
.note {
  font-size: 13px;
  color: var(--text-3);
  margin-top: 8px;
}
.file-error {
  margin-top: 10px;
  font-size: 13px;
  color: var(--danger);
}
/* 拉取框里的文件清单：一个文件一个小方框，只写文件标识；行、列间距都放大一些
   （原来 6px 的间距在多份文件时糊成一片）。其余信息看悬浮提示。 */
.pull-summary {
  margin-bottom: 10px;
}
.pull-files {
  display: flex;
  flex-wrap: wrap;
  gap: 14px 12px;
}
.pull-chip {
  display: inline-block;
  max-width: 260px;
  padding: 4px 12px;
  border: 1px solid color-mix(in srgb, var(--accent) 45%, var(--line));
  border-radius: 6px;
  background: color-mix(in srgb, var(--accent) 7%, var(--bg-raised));
  font-size: 13px;
  line-height: 1.6;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  cursor: default;
  transition: border-color 0.15s ease, color 0.15s ease, background 0.15s ease;
}
.pull-chip:hover {
  border-color: var(--accent);
  color: var(--accent);
}
/* 「还有 N 份…」不是文件，框子用虚线、不抢颜色。 */
.pull-chip.is-more {
  border-style: dashed;
  border-color: var(--line);
  background: transparent;
  color: var(--text-3);
}
/* ---------- 芯片的悬浮提示：纵向成列、左右两列对齐 ----------
   ★ el-tooltip 的内容会被 teleport 到 body，scope 的 data-v 属性挂不上去，
     所以这一块必须用 :global() 写，否则样式一条都不生效。
   ★★ 底色 / 描边 / 文字色全部**写死**，不用主题变量：冒泡层自带一套
     颜色（实测：白底 + 浅色字，结果字根本看不见）。这里固定成深色面板，
     明暗主题下都能看清。
   ★ 选择器带上 `.is-dark`：Element Plus 自己的 `.el-popper.is-dark` 权重
     更高，不写这半会盖不过去。 */
:global(.vds-chip-tip.el-popper),
:global(.vds-chip-tip.el-popper.is-dark) {
  max-width: 380px;
  padding: 10px 12px;
  border: 1px solid rgba(120, 190, 255, 0.35);
  background: #0d1621;
  color: #e8f1fb;
  box-shadow: 0 12px 30px rgba(0, 0, 0, 0.45);
  font-size: 12px;
  line-height: 1.5;
}
:global(.vds-chip-tip .el-popper__arrow::before),
:global(.vds-chip-tip.is-dark .el-popper__arrow::before) {
  background: #0d1621;
  border-color: rgba(120, 190, 255, 0.35);
}
:global(.vds-chip-tip .tip-rows) {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
:global(.vds-chip-tip .tip-row) {
  display: grid;
  grid-template-columns: 58px minmax(0, 1fr);
  gap: 12px;
  align-items: baseline;
}
/* 标签列：灰蓝，和值拉开层次，但保证在深底上看得见。 */
:global(.vds-chip-tip .tip-row > span) {
  color: #8fa6bf;
}
/* 值列：接近白，加粗一点。 */
:global(.vds-chip-tip .tip-row > b) {
  color: #eaf3ff;
  font-weight: 500;
  word-break: break-all;
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
  font-size: 13px;
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
  font-size: 13px;
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
/* ★ 筛选条件用**网格**排，不用 flex 换行：每项一格、标签右对齐且定宽，
   所以不管窗口多宽，控件都落在同一条竖线上，不会错落地挤在一起。
   `auto-fill` + `minmax` 让它自己决定一行放几组（窄屏就少放几组）。 */
.filters {
  /* ★ 固定两列：第一行“所有者 / 文件标识”，第二行“块数 / 大小”。两边宽度变了
     也不重排（原来是 auto-fill，左栏一变宽就会变成三列）。 */
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 20px;
  align-items: center;
}
/* 一个「指标 + 它要填的值」= 一组。组内左标签、右控件。 */
.flt {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
/* ★ 宽 68px 是为了容下最长的两个标签（「文件标识」四字、「大小/KB」）。
   定宽 + 右对齐 ⇒ 同一列的所有标签右边缘落在同一条竖线上。 */
.flt-lbl {
  flex: none;
  width: 68px;
  text-align: right;
  font-size: 13px;
  color: var(--text-3);
  white-space: nowrap;
}
/* 区间类指标（块数 / 大小）：两个输入 + 中间的连接符，平分剩余宽度。 */
.flt-range {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1 1 0;
  min-width: 0;
}
.flt-range .el-input-number {
  flex: 1 1 0;
  min-width: 0;
}

/* 筛选 + 集合：一左一右两栏，左栏约占 2/3，右栏 1/3；两栏**高度始终一致**。 */
.two-col {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}
.two-col > .panel { margin-bottom: 0; }
@media (max-width: 1180px) {
  .two-col { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 1080px) {
  .two-col { grid-template-columns: minmax(0, 1fr); }
}

/* 逐块归属：条数多时默认收起，展开后限高滚动（不让它把页面撞长）。 */
.refs-collapse { margin-top: 10px; }
.refs-collapse > summary {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 0;
  font-size: 13px;
  color: var(--text-2);
  cursor: pointer;
  list-style: none;
}
.refs-collapse > summary::-webkit-details-marker { display: none; }
.refs-collapse .tri { font-size: 10px; color: var(--text-3); transition: transform 0.18s ease; }
/* 折叠标题右侧那半句说明：比标题再淡一档，不抢“逐块明细（N 块）”这个主信息。 */
.refs-collapse .sum-note { color: var(--text-3); font-size: 12px; margin-left: 4px; }
.refs-collapse[open] > summary .tri { transform: rotate(90deg); }
.refs-collapse .refs { max-height: 300px; overflow: auto; }
/* 每行留白随内容自适应：行高固定，但上下留白给足，不挤在一起。 */
.refs .ref-row { padding: 3px 0; line-height: 1.7; }
/* 一行一份文件：每列定宽，上下逐列对齐；名字列吃掉剩余空间，
   过长时未尾省略（鼠标悬浮 title 里给全名）。 */
.file-row {
  display: grid;
  grid-template-columns: 20px 84px minmax(0, 1fr) 58px 72px 42px 126px max-content max-content;
  gap: 8px;
  align-items: center;
  font-size: 13px;
  padding: 3px 0;
}
.file-row .f-owner,
.file-row .f-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.file-row .f-owner { color: var(--text-3); }
.tag-ok {
  color: var(--accent);
  font-size: 13px;
}
/* 集合的极简视图：一行下标芯片，主界面只给这么多 */
.basket-mini {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}
.chip {
  font-size: 13px;
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
  font-size: 13px;
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
  font-size: 13px;
  padding: 1px 6px;
  border: 1px solid var(--line);
  border-radius: 8px;
}
/* 主流程步骤徽标：快速验证 / 选块入集合 / 统一验证 / 内部坐标 */
.step-tag {
  flex-shrink: 0;
  font-size: 13px;
  line-height: 1;
  padding: 3px 8px;
  border-radius: 999px;
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}
</style>
