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
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { evidenceApi } from '../../../api/evidence'
import { filesApi } from '../../../api/files'
import { useAuthStore } from '../../../stores/auth'
import { useBasketStore } from '../../../stores/basket'
import { usePoolStore } from '../../../stores/pool'
import { useThemeStore } from '../../../stores/theme'
import { systemApi } from '../../../api/system'
import { fmtBytes, span } from '../../../utils/format'
import { MAX_QUERY_INDICES } from '../../../utils/constants'
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

/**
 * 当前**存活下标的最大值**（所有文件 ``last_index`` 里最大的那个）。
 *
 * ★★ 判“这份文件能不能**整份删掉**”要看它，**不能**用 `blocks - 1`：
 *   删过中间那份文件之后会留下空洞（段永不回收），于是 `blocks` 会
 *   **小于**某个仍然有效的 `last_index` —— 结果唯一那份删得动的文件反而
 *   被标成“不可删”，“只看末尾可删”这个筛选恒为空（审计 F4，实测复现）。
 *
 *   ``slots_allocated``（= `registry.next_offset`）也不能当它用：
 *   那是“下一个待分配的槽位”，比存活最大值还大。
 */
const maxLiveIndex = computed(() =>
  allFiles.value.reduce((m, f) => Math.max(m, Number(f.last_index ?? -1)), -1),
)

//: 拉取框里最多铺多少个文件芯片，剩下的折成一句「还有 N 份…」。
const PULL_CHIP_MAX = 24
const pullChips = computed(() => allFiles.value.slice(0, PULL_CHIP_MAX))
const pullRest = computed(() => Math.max(0, allFiles.value.length - PULL_CHIP_MAX))

/**
 * 给每个文件补两个**派生字段**（后端不提供，前端算）：
 *
 * * ``inBasket`` —— 这份文件已经有多少块在集合里；
 * * ``tailDeletable`` —— 它能不能**整份删掉**（该文件拥有当前**存活**下标的最大值）。
 *   方案只允许从向量末尾往前删，所以只有“排在最后”的那份文件删得动
 *   （见后端 files.py 的删除说明）。判据必须是 ``maxLiveIndex``，
 *   不是 `blocks - 1` —— 理由见上面那段。
 */
const rows = computed(() =>
  allFiles.value.map((f) => {
    const indices = f.indices || []
    return {
      ...f,
      inBasket: basket.countOf(indices),
      tailDeletable: indices.length > 0 && Number(f.last_index) === maxLiveIndex.value,
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
      // 旧判据的傅底（拿不到 /api/files 时用）
      basket.syncN(n)
    }
    // ★ 更准的那一份：**当前还活着哪些下标**（各文件 ``indices`` 的并集）。
    //   集合据此标出“这几块已经被删了” —— 只看总数会被“删中间留下的空洞”
    //   骗到：活着的块被标红、真删的反而标不出来（审计 F3）。
    const live = new Set()
    for (const f of list) for (const i of f.indices || []) live.add(i)
    basket.syncLive([...live])
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
 * 文件列表里的「只收几块…」：就地弹一个输入框，只把这几块收进集合。
 *
 * 不另开弹窗组件 —— 要填的东西就一个“第几块”，用 Element Plus 自带的
 * ``ElMessageBox.prompt`` 最省事，也不会再多一块需要维护的 UI。
 */
async function addBlocksPrompt(f) {
  let value = ''
  try {
    const r = await ElMessageBox.prompt(
      `这份文件共 ${f.block_count} 块。填第几块（如 0-3, 8），只把这几块收进集合。`,
      `只收几块：${f.owner} / ${f.file_key}`,
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

/** 报 告里"集合中没被这次结论覆盖"的下标（响应缺的 + 已经被删掉的）。 */
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
      const name = `${r.owner} / ${r.file_key}`
      const e = byFile.get(name) || { name, blocks: [] }
      e.blocks.push(Number(r.block_idx))
      byFile.set(name, e)
    }
    return [...byFile.values()].map((e) => ({
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
      name: `${f.owner} / ${f.file_key}`,
      blocks: hit.length,
      scope: hit.length ? `本次覆盖第 ${span(hit)} 块` : '本次覆盖 0 块',
    }
  })
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

// 进入页面即自动拉取文件列表（列表是所有后续操作的前提）。
onMounted(loadFiles)
</script>

<template>
  <div class="verify-page">
    <PageHeader title="完整性验证" subtitle="公开验证；解密需所有权" />

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
        <div class="pull-summary mono text-2" style="font-size: 12px">
          已拉取 {{ allFiles.length }} 份文件 · 共 {{ totalBlocks }} 块 · {{ owners.length }} 位所有者
        </div>
        <div class="pull-files">
          <span v-for="f in pullChips" :key="f.id" class="pull-chip mono">
            {{ f.owner }}/{{ f.file_key }} · {{ f.block_count }} 块
          </span>
          <span v-if="pullRest" class="pull-chip mono text-3">还有 {{ pullRest }} 份…</span>
        </div>
        <p class="text-3" style="font-size: 12px; margin: 8px 0 0">
          下面「按指标筛选文件」用的就是这份清单 —— 筛选全在前端，不会再请求后端。
        </p>
      </template>

      <div v-else-if="filesLoading" class="text-3" style="font-size: 12px">正在拉取…</div>
      <div v-else class="text-3" style="font-size: 12px">列表为空。</div>
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
        <el-button :disabled="advancedMode ? false : !verifyFileId" @click="addToBasket">
          加入集合
        </el-button>
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
              这一份覆盖 {{ result.indices?.length || 0 }} 块
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
            <span class="mono">第 {{ r.block_idx }} 块</span>
            <span class="text-2">→ {{ r.owner }} / {{ r.file_key }}</span>
            <span class="mono text-3">holder: {{ result.holders?.[r.global_index] }}</span>
          </div>
        </div>
        <StageTimeline v-if="result.timings" :timings="result.timings" class="mt-2" />
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
    <!-- ② 按指标筛选文件                                                -->
    <!-- ============================================================= -->
    <div class="panel mb-3">
      <div class="sec-head">
        <h4 class="sec-title">按指标筛选文件</h4>
        <span class="step-tag">选块入集合</span>
      </div>

      <!-- ★ 每个指标一组：`.flt` 里左边是定宽右对齐的标签、右边是控件。
           用网格排（见 <style> 里的 `.filters`），所以所有标签与控件都对到同一条竖线。 -->
      <div class="filters">
        <div class="flt">
          <span class="flt-lbl">所有者</span>
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

        <div class="flt">
          <span class="flt-lbl">版本 ≥</span>
          <el-input-number
            v-model="filters.minVersion"
            :min="0"
            :controls="false"
            placeholder="不限"
            style="width: 100%"
          />
        </div>

        <div class="flt">
          <span class="flt-lbl">排序</span>
          <el-select v-model="filters.sortBy" style="width: 100%">
            <el-option label="按编号" value="id" />
            <el-option label="按块数" value="blocks" />
            <el-option label="按大小" value="bytes" />
            <el-option label="按版本" value="version" />
            <el-option label="按已入集合" value="basket" />
          </el-select>
        </div>
      </div>

      <div class="flt-switches">
        <el-checkbox v-model="filters.onlyDecryptable">只看我能解密的</el-checkbox>
        <el-checkbox v-model="filters.excludeBasket">只看还没全收进集合的</el-checkbox>
        <el-checkbox v-model="filters.onlyTail">只看能整份删掉的（排在向量末尾）</el-checkbox>
      </div>

      <div class="flt-presets">
        <span class="flt-lbl" style="width: auto">快捷</span>
        <el-button size="small" @click="presetMine">只看我的</el-button>
        <el-button size="small" @click="presetAllInBasketTail">末尾且未入集合</el-button>
        <el-button size="small" @click="resetFilters">重置筛选</el-button>
      </div>

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
            <span v-if="f.can_decrypt" class="tag-ok">可解密</span>
            <span v-if="f.inBasket" class="tag-ok mono">已入集合 {{ f.inBasket }}/{{ f.block_count }}</span>
            <el-button link type="primary" size="small" @click="basket.addFile(f)">收整份</el-button>
            <el-button link type="primary" size="small" @click="addBlocksPrompt(f)">只收几块…</el-button>
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
        列表未加载。请执行「重新拉取」。
      </div>

      <details class="note-collapse">
        <summary>说明</summary>
        <p class="note">
          用上面的条件筛选文件，勾选后点「把勾选的…收进集合」，或点每行的「收整份」「只收几块…」
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

      <div v-if="basketResult" class="mt-3">
        <VerifyResult :result="basketResult" />
        <div class="cur-file">
          <span class="cur-lbl">报告</span>
          <span class="cur-item mono">覆盖 {{ basketResult.indices?.length || 0 }} 块</span>
          <span class="cur-item mono">证据 {{ basketResult.proof?.size_bytes }} 字节</span>
          <span class="cur-item mono">{{ basketHolderCount }} 台节点参与</span>
          <span class="cur-total mono">{{ basket.fileCount }} 份文件</span>
        </div>
        <div v-if="basketFiles.length > 1" class="cur-file">
          <span class="cur-lbl">这份证据覆盖</span>
          <span v-for="f in basketFiles" :key="f.name" class="cur-item mono">
            {{ f.name }}<span class="text-3"> · {{ f.scope }}</span>
          </span>
        </div>
        <div v-if="basketMissed.length" class="missing">
          <el-alert
            type="warning"
            :closable="false"
            :title="`这次的结论没覆盖集合里的 ${basketMissed.length} 块：${basketText(basketMissed)}`"
          />
        </div>
        <div v-if="detailed || basketDetailed" class="refs mt-2">
          <div v-for="r in basketRefs" :key="r.global_index" class="ref-row">
            <span class="mono">第 {{ r.block_idx }} 块</span>
            <span class="text-2">→ {{ r.owner }} / {{ r.file_key }}</span>
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

      <details class="note-collapse">
        <summary>说明</summary>
        <p class="note">
          把要验证的块收进集合后，点「验证集合里的全部块」，一次验完集合里的所有块，结果显示在下方。
        </p>
      </details>
    </div>

    <!-- ============================================================= -->
    <!-- ④ 查存储槽位（内部坐标工具，排最后）                            -->
    <!-- ============================================================= -->
    <div class="panel mb-3">
      <div class="sec-head">
        <h4 class="sec-title">查存储槽位</h4>
        <span class="step-tag">内部坐标</span>
      </div>
      <div class="query-form">
        <el-input v-model="regIndex" placeholder="槽位号，如 3" style="width: 160px" />
        <el-button type="primary" :loading="regRunning" @click="runRegistry">查询</el-button>
      </div>
      <div v-if="regResult" class="mt-3">
        <template v-if="!regResult.error">
          <div class="mono text-1" style="font-size: 13px">
            槽位 {{ regResult.global_index }} → {{ regResult.owner }} / {{ regResult.file_key }} 的第 {{ regResult.block_idx }} 块
          </div>
          <div class="text-2 mono" style="font-size: 12px">
            holder: {{ regResult.holder }} · replicas: {{ (regResult.replicas || []).join(', ') }}
          </div>
        </template>
        <div v-else class="text-danger">{{ regResult.error }}</div>
      </div>

      <details class="note-collapse">
        <summary>说明</summary>
        <p class="note">
          输入存储槽位号，点「查询」，查看该槽位存放的是哪个文件的哪一块、存放在哪些节点。
          这是排查用的辅助工具，常规验证不需要用到。
        </p>
      </details>
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
/* 拉取框里的文件清单：芯片式铺开，一行放不下就换行；太长单个芯片自己截断。 */
.pull-summary {
  margin-bottom: 8px;
}
.pull-files {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.pull-chip {
  display: inline-block;
  max-width: 100%;
  padding: 2px 9px;
  border: 1px solid rgba(127, 127, 127, 0.28);
  border-radius: 999px;
  font-size: 12px;
  line-height: 1.7;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
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
/* ★ 筛选条件用**网格**排，不用 flex 换行：每项一格、标签右对齐且定宽，
   所以不管窗口多宽，控件都落在同一条竖线上，不会错落地挤在一起。
   `auto-fill` + `minmax` 让它自己决定一行放几组（窄屏就少放几组）。 */
.filters {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(290px, 1fr));
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
  font-size: 12px;
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
/* 开关类条件：用一条细分隔线与上面的数值型隔开，免得混成一堆。 */
.flt-switches {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 18px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed rgba(127, 127, 127, 0.28);
}
.flt-presets {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 8px;
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
/* 主流程步骤徽标：快速验证 / 选块入集合 / 统一验证 / 内部坐标 */
.step-tag {
  flex-shrink: 0;
  font-size: 11px;
  line-height: 1;
  padding: 3px 8px;
  border-radius: 999px;
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
}
</style>
