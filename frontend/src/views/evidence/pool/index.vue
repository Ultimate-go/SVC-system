<script setup>
/**
 * 证据池。
 *
 * - 池子存 sessionStorage（关掉标签页就没了，刻意）。
 * - 每张卡片记录 indices / values / proof / delta_fp / delta_n / 取回时间。
 * - 按 delta_fp 判断作废（不是 n）。
 * - 「一次验这 N 份」→ verify-batch，两个耗时都显示，agree 不一致报警。
 * - 分解再聚合 4 步演示。
 * - **逐块指纹**：每张卡片可以展开看"这份证据覆盖的每个下标，它的分量指纹是多少"
 *   —— 跟着全局的「简略/详细」开关，也能在单张卡片上自己开合。
 */
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { usePoolStore } from '../../../stores/pool'
import { useThemeStore } from '../../../stores/theme'
import { evidenceApi } from '../../../api/evidence'
import { filesApi } from '../../../api/files'
import { systemApi } from '../../../api/system'
import { span, fmtAgo } from '../../../utils/format'
import { MAX_QUERY_INDICES } from '../../../utils/constants.js'
// ★ 界面上要的是"第几块"（parseBlockRange），不是内部坐标（parseIndexRange）
import { parseBlockRange } from '../../../utils/validate'
import PageHeader from '../../../components/common/PageHeader.vue'
import EmptyState from '../../../components/common/EmptyState.vue'
import DetailToggle from '../../../components/common/DetailToggle.vue'
import VerifyResult from '../../../components/security/VerifyResult.vue'
import BlockFingerprints from '../../../components/security/BlockFingerprints.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'

const pool = usePoolStore()
const theme = useThemeStore()

/** 详细模式：卡片默认把逐块指纹展开。 */
const detailed = computed(() => theme.detailMode === 'detail')

/**
 * 单张卡片的开合**覆盖**：`undefined` = 跟着详细模式走。
 *
 * ★ 做成三态（真/假/未设）而不是一个布尔：因为"详细模式默认全展开"
 *   与"我就想把这单独一张收起来"必须能同时成立 —— 一个布尔就会
 *   把前者吞掉（点一下收起，切个页又自己开回来）。
 */
const openCards = ref({})
function isOpen(card) {
  const o = openCards.value[card.id]
  return o === undefined ? detailed.value : o
}
function toggleCard(card) {
  openCards.value[card.id] = !isOpen(card)
}

const batchRunning = ref(false)
const batchResult = ref(null)

/* ---- 取回：**选文件 + 第几块**（主交互）----
 *
 * ★ 为什么不再让用户填"全局下标"：那是**内部坐标**，它由上传顺序决定，
 *   和用户脑子里想的"这份文件的第几块"是两码事。把内部坐标露到界面上，
 *   用户就得自己去数前面的文件占了多少位置 —— 错一次还很难看出来。
 *   现在界面上只有文件名与块号，转换成全局下标是前端自己的事。
 */
/*
 * ★ 这里原本有一块「取回：选文件 + 第几块」的交互（文件下拉、块号输入、
 *   「取回证据」按钮），已经**删掉**了：它与「文件与块」页的「入池 / 一键入池」
 *   是同一件事，两处并存只会让人猜哪个才算数。
 *
 *   **保留**的是 `/api/query/files` 这条路本身（`evidenceApi.queryFiles`）——
 *   「分解（跨文件）」在用它（见 `confirmCrossDisagg`）。
 */

const disaggRunning = ref(false)
const disaggResult = ref(null)

/* ---- 分解：在**这张卡覆盖的下标**上选「从哪分到哪」 ----
 * 以前固定「丢掉最后一个下标」，等于没得选；现在范围由用户定。 */
const disaggOpen = ref(false)
const disaggCardId = ref('')
const disaggFrom = ref(null)
const disaggTo = ref(null)
//: 完整下标默认**收起**（块数多时会超出范围，展开后也是可滚动的）
const showAllIndices = ref(false)

/** 对话框里那张卡（默认 = 勾选的第一张）。 */
const disaggCard = computed(
  () => pool.selectedCards.find((c) => c.id === disaggCardId.value) || pool.selectedCards[0] || null,
)
/** 这张卡覆盖的下标（升序）—— 范围就在这个序列上选。 */
const disaggIndices = computed(() =>
  [...(disaggCard.value?.indices || [])].sort((a, b) => a - b),
)

/** 分解弹框里每一项的**显示文字**（**值仍然是全局下标** —— 那是发给后端的）。 */
const disaggIdxMap = computed(() => indexToBlockMap(disaggCard.value))
function idxLabel(i) {
  const b = disaggIdxMap.value.get(Number(i))
  return b === undefined ? String(i) : `第 ${b} 块`
}
const fromPos = computed(() => disaggIndices.value.indexOf(disaggFrom.value))
const toPos = computed(() => {
  const n = disaggIndices.value.length
  const i = disaggIndices.value.indexOf(disaggTo.value)
  return i < 0 ? n - 1 : i
})
/** K = 从 from 到 to 的那一段；两头填反了自动对调（不让用户白填一次）。 */
const disaggK = computed(() => {
  const a = Math.max(0, Math.min(fromPos.value, toPos.value))
  const b = Math.max(fromPos.value, toPos.value)
  return disaggIndices.value.slice(a, b + 1)
})
const disaggDropped = computed(() =>
  disaggIndices.value.filter((i) => !disaggK.value.includes(i)),
)

const isStale = (card) => pool.staleIds.has(card.id)

/**
 * 卡片标题 —— 下标在**显示时**折起来，不把 1, 2, 3, 4… 平铺出来。
 *
 * ★★ 压缩必须发生在显示这一层，而且拿 `card.indices` **现算** ——
 *   不看 label 里当初是怎么写的。
 *
 *   理由：池子存在 sessionStorage 里，**已经取回来的卡**的 label 是写死的
 *   字符串（「完整性验证」页当初用 indices.join(',') 拼的，长这样：
 *   「查询：0, 1, 2, …, 100」）。只把那边的写法改对救不了这些旧卡 ——
 *   只有显示时重算，同一批下标在池子里才永远长得一样。
 *
 *   label 从此只承担「这张卡从哪来 / 是哪个文件」：冒号前那截当前缀，
 *   冒号后面那串历史下标直接丢掉。
 *
 * ★ 所以本改动**只落在这一个文件**：`verify/index.vue` 存进来的 label
 *   仍然带着旧下标，但它显示时会被这里覆盖，那边不必动。
 */
function cardScope(card) {
  // ★ 优先用"哪份文件的第几块"说话（界面坐标）；只有旧卡（没存 files）
  //   才退回内部坐标。这样新卡片上根本不会出现"全局下标"这种字样。
  const files = card.files || card.result?.files || null
  if (Array.isArray(files) && files.length) {
    return files
      .map((f) => {
        // ★ 这份文件的「第几块」优先用后端给的 ``block_indices``
        //   （``/api/query/files`` 会给）；``/api/query`` 的 ``files`` 只带
        //   ``offset`` / ``n``，缺了就得**现算** —— 否则卡片标题会退化成
        //   「第 — 块」（实测）：本文件的位置区间是
        //   ``[offset, offset + n)``，块号 = 全局位置 − offset。
        let bs = f.block_indices
        if (!Array.isArray(bs) || !bs.length) {
          const off = Number(f.offset)
          const nf = Number(f.n)
          if (Number.isFinite(off) && Number.isFinite(nf)) {
            bs = (card.indices || [])
              .filter((i) => i >= off && i < off + nf)
              .map((i) => i - off)
          }
        }
        return `${f.file_key} 第 ${span(bs || [])} 块`
      })
      .join('；')
  }
  return span(card.indices)
}

/**
 * 这张卡上「全局下标 → 归属」的换算表：**哪份文件的第几块**（+ 所有者）。
 *
 * ★★ 为什么需要它：`/evidence/disagg` 的输入 K **必须**是全局下标
 *   （它是在一条向量上做代数）。但全局下标是**内部坐标** —— 它由上传顺序
 *   决定，既不该让人记、也不该出现在界面上（见本文件开头那段）。
 *   所以规矩是：**内部的数留在内部，显示那一层一律换回“哪份文件的第几块”**。
 *   （用户反馈：分解弹框里还在印 3159、3160 这种数。）
 *
 * 三种来源，按可靠度排：
 *
 * ① `result.refs` —— 后端逐下标给的归属，最权威（`/api/query` 会给）；
 * ② `files[].block_indices` 与 `files[].indices`（`/api/query/files` 会给，两者同序）；
 * ③ 本文件的位置区间是 `[offset, offset+n)`，块号 = 全局位置 − offset。
 *
 * 三样都没有（很旧的卡）⇒ 空表，显示层如实退回数字，**不猜**。
 *
 * ★ 为什么带上 `owner` / `file_key`：跨文件的卡（合并向量）上，两段都从
 *   「第 0 块」数起 —— 不写清是哪份文件，一屏就是一行行重复的「第 0 块」
 *   （实测撞到过）。所以换算表连归属一起给，显示层才有得区分。
 */
function indexToRefMap(card) {
  const map = new Map()
  if (!card) return map
  for (const r of card.result?.refs || []) {
    const b = Number(r.block_idx)
    if (!Number.isFinite(b)) continue // 归属给了但块号缺：宁可不填，也别塞个 NaN
    map.set(Number(r.global_index), {
      block_idx: b,
      owner: r.owner || '',
      file_key: r.file_key || '',
    })
  }
  if (map.size) return map
  for (const f of card.files || card.result?.files || []) {
    const who = { owner: f.owner || '', file_key: f.file_key || '' }
    const bs = f.block_indices
    const gs = f.indices
    if (Array.isArray(bs) && Array.isArray(gs) && bs.length && bs.length === gs.length) {
      gs.forEach((g, k) => map.set(Number(g), { block_idx: Number(bs[k]), ...who }))
      continue
    }
    const off = Number(f.offset)
    const nf = Number(f.n)
    if (Number.isFinite(off) && Number.isFinite(nf)) {
      for (const g of card.indices || []) {
        if (g >= off && g < off + nf) map.set(Number(g), { block_idx: g - off, ...who })
      }
    }
  }
  return map
}

/** 只要块号的那份投影（分解弹框、卡片标题用）。 */
function indexToBlockMap(card) {
  const map = new Map()
  for (const [g, r] of indexToRefMap(card)) map.set(g, r.block_idx)
  return map
}

/**
 * 给 `<BlockFingerprints>` 用的 `refs`（下标 → 块号 + 归属）。
 *
 * ★ 为什么要补：组件靠 `refs` 才能把行头写成「第几块」。池子这边原来**没传**，
 *   于是它退回了数字 —— 一屏的全局下标（用户反馈看到的正是这个：盘古石那份
 *   77 块，行头却是 12…88）。验证页两处都传了，这里漏了。
 *
 * 没有归属信息（很旧的卡）就传空数组：组件那边会如期退回数字，不编块号。
 */
function fpRefs(card) {
  const m = indexToRefMap(card)
  if (!m.size) return []
  return (card.indices || []).map((i) => {
    const r = m.get(Number(i))
    return r
      ? { global_index: i, block_idx: r.block_idx, owner: r.owner, file_key: r.file_key }
      : { global_index: i, block_idx: null }
  })
}

/** 一组全局下标 → 它们对应的**块号**（有一个算不出来就返回 null）。 */
function blocksOf(card, list) {
  const gs = Array.isArray(list) ? list : []
  if (!gs.length) return null
  const map = indexToBlockMap(card)
  if (!map.size) return null
  const out = []
  for (const g of gs) {
    const b = map.get(Number(g))
    if (b === undefined) return null // 有一个算不出来就整组退回数字，不混着说
    out.push(b)
  }
  return out
}

/**
 * 把一组全局下标用**界面坐标**说出来。
 *
 * :param expand: ``true`` = 逐个列出块号；``false`` = 压成区间（「第 0-3 块」）。
 * :returns: 换算不了时如实退回数字（不假装它是个块号）。
 */
function blockText(card, list, expand = false) {
  const gs = Array.isArray(list) ? list : []
  if (!gs.length) return '（空）'
  const bs = blocksOf(card, gs)
  if (!bs) return expand ? gs.join(', ') : span(gs)
  return `第 ${expand ? bs.join(', ') : span(bs)} 块`
}

function cardTitle(card) {
  const scope = cardScope(card)
  const raw = String(card.label || '')
  if (!raw) return scope
  const cut = raw.search(/[：:]/)
  return `${cut >= 0 ? raw.slice(0, cut) : raw}：${scope}`
}

/**
 * 「看完整标题」。
 *
 * ★ 标题在卡片上是**单行省略**的 —— 否则像
 *   「聚合：test_10KB 第 0-10 块；test_20KB 第 0-19 块；test_50KB 第 0-49 块」
 *   这种长标题会把 grid 的列撑开、压到右边相邻的那张卡上。
 *
 * ★ 为什么不用原生 `title`：它只在**鼠标悬停**时出现 —— 触屏看不到，
 *   想把这串东西复制下来也选不中。所以给一个点得开的入口，
 *   字可以直接选中、复制。
 */
function showFullTitle(card) {
  ElMessageBox.alert(cardTitle(card), '完整标题', {
    confirmButtonText: '关闭',
  })
}

async function syncDelta() {
  try {
    const { data } = await systemApi.status()
    // ★★ 判“这张卡还有效吗”靠的是**每份文件**的 δ 指纹，不是长度：
    //   改块 / 清零 / 追加 / 截断都只改 C，**不改全局位置总数 n** ——
    //   只比 n 的话，改完块池子里的卡看起来一切正常，实际已经验不过了。
    //   （以前这里传的是 `fp: null`，于是那条按指纹的判据从来没生效过。）
    pool.syncDelta({
      fp: data?.delta?.fp ?? null,
      n: data.delta.n,
      fileFps: Object.fromEntries(
        (data?.delta?.files || []).map((f) => [`${f.owner}/${f.file_key}`, f.delta_fp]),
      ),
    })
    // 副本数：取回耗时的口径之一（每块要问持它的那 r 台）。
    // 拿不到就留 null —— 界面上宁可不显示这个数，也不要显示一个编的。
    //
    // ★ 后端给的字段名是 **replica_factor**（顶层），不是 `crs.replicas`
    //   —— 以前读错了名，于是这个数**恒为 null**："每块要问几台、共几次模幂"
    //   永远显示不出来（安全审计 P4）。旧名保留在后备位，兼容可能的老响应。
    // ★ 顶层那个取回区已经删了（改用文件列表的「入池 / 一键入池」），
    //   `replica_factor` 暂时没有展示位，这里不再读它（那个 ref 也一并删了）。
    // 也取指纹：status 没有 delta_fp，用 query 一个空集取不到 —— 直接用 n 与已有卡比对。
    // 实际上 delta_fp 需要从某次 query 拿。这里从池子里已有的 fp 兜底。
  } catch {
    /* 忽略 */
  }
}

/**
 * 「聚合选中」—— 两条路的语义不同，**必须分开报**（详见 `stores/pool.js::aggregateSelected`）：
 * 同一份文件内是真聚合（VC.Agg，只收凭证）；跨文件是「归约」（取密文 + 重算）。
 * 报错时也不能只说“失败”—— 要告诉用户这一次到底发生了什么。
 */
const aggregating = ref(false)
async function aggregateCards() {
  if (!pool.selectedCards.length) return
  const files = pool.unionScope.files
  aggregating.value = true
  try {
    const card = await pool.aggregateSelected()
    if (files.size > 1) {
      ElMessage.success(
        `已归约：${files.size} 份文件 → 一条合并向量上的证据（256 字节，覆盖 ${card?.indices?.length ?? 0} 个位置），仍可再聚合、可分解`,
      )
    } else {
      ElMessage.success(`已聚合 ${pool.selectedCards.length} 张卡（同一份文件内，走 VC.Agg）`)
    }
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '聚合失败')
  } finally {
    aggregating.value = false
  }
}

/**
 * 跨文件被拒后的下一步：先把池子里的卡归约成一条合并向量上的证据。
 *
 * ★ 归约完那张新卡就落在“一份文件”的世界里 —— 之后它自己可验、可批量、可分解
 *   （也就是论文 `StrgNode.CreateFrom` 之后“一切照旧”的顺序）。
 */
async function mergeNow() {
  await aggregateCards()
  batchResult.value = null
}

async function batchVerify() {
  const cards = pool.selectedCards
  if (!cards.length) {
    ElMessage.warning('先勾选几张卡片')
    return
  }
  batchRunning.value = true
  batchResult.value = null
  try {
    const items = cards.map((c) => ({
      indices: c.indices,
      values: c.result.values,
      proof: c.result.proof,
    }))
    const { data } = await evidenceApi.verifyBatch(items, true, true)
    batchResult.value = data
  } catch (e) {
    const detail = e?.response?.data?.detail || '批量验证失败'
    // ★ 只有“卡来自不同宇宙”时后端才拒绝 —— 这不是故障（Def. 29 要求同一个承诺 C），
    //   所以不能只把错误原样弹出来，要给出可执行的下一步。
    //   注意：一张“归约出来的跨文件卡”本身是可以一次结论的（它已经落在一条向量上）。
    const needsMerge = /只支持「一份文件」|跨了\s*\d+\s*份文件|不同的合并向量/.test(detail)
    batchResult.value = { ok: false, message: detail, needsMerge }
  } finally {
    batchRunning.value = false
  }
}

/** 这张卡覆盖的**文件**（去重后的 "owner/file_key" 列表）。 */
function cardFiles(card) {
  const fs = card?.files || card?.result?.files || []
  return [...new Set(fs.map((f) => `${f.owner} / ${f.file_key}`))]
}

/**
 * 这张卡在**每份文件**上覆盖的块号 —— 每份各自一份清单。
 *
 * ★ 为什么需要它：**聚合**出来的卡，每份文件覆盖的块号是**不同**的
 *   （例如「50KB 第 0-49 块 + 64KB 第 0-63 块」）。以前只能按“共用块号”
 *   取回，于是这种卡**重现不出来** —— 点“取回”拿到的是顶部文件框里选的那一份，
 *   看上去就像接口拿错了东西。
 *
 *   块号优先用后端给的 `block_indices`；旧卡没存这个字段就用 `offset`/`n`
 *   与卡自己的 `indices` 现算（与本文件 `cardScope` 同一套逻辑）。
 */
function cardBlocksPerFile(card) {
  const fs = card?.files || card?.result?.files || []
  return fs.map((f) => {
    let bs = f.block_indices
    if (!Array.isArray(bs) || !bs.length) {
      const off = Number(f.offset)
      const nf = Number(f.n)
      if (Number.isFinite(off) && Number.isFinite(nf)) {
        bs = (card.indices || []).filter((i) => i >= off && i < off + nf).map((i) => i - off)
      }
    }
    return Array.isArray(bs) ? [...bs].sort((a, b) => a - b) : []
  })
}

/**
 * 卡上**第 i 份文件**覆盖的那些**全局下标**（必是 `card.indices` 的子集）。
 *
 * ★ 跨文件分靠就靠它：跨文件卡是在一条**合并向量**上开的证据，
 *   而“某一份文件”在那条向量里占的位置段就是 `[offset, offset+n)`。
 *   把这区间与卡的 `indices` 求交，就得到“这份文件在这张卡里的那部分”。
 *   后端 `/evidence/disagg` 只做代数（只要 K ⊆ I），
 *   所以这条路不需要后端任何改动。
 */
function indicesOfFileInCard(card, i) {
  const fs = card?.files || card?.result?.files || []
  const f = fs[i]
  if (!f) return []
  const off = Number(f.offset)
  const nf = Number(f.n)
  if (!Number.isFinite(off) || !Number.isFinite(nf)) return []
  return (card.indices || []).filter((g) => g >= off && g < off + nf).sort((a, b) => a - b)
}

/* ---- 分解（跨文件）：选一份文件，把它在卡里的那部分拆出来（用户反馈）---- */
const crossOpen = ref(false)
const crossRunning = ref(false)
const crossCardId = ref('')
const crossFileIdx = ref(null)

const crossCard = computed(
  () => pool.selectedCards.find((c) => c.id === crossCardId.value) || null,
)
const crossFileList = computed(() => {
  const c = crossCard.value
  const fs = c?.files || c?.result?.files || []
  return fs.map((f, i) => ({
    i,
    owner: f.owner,
    file_key: f.file_key,
    blocks: indicesOfFileInCard(c, i).length,
  }))
})
/**
 * 选中的那份文件，在这张卡里覆盖的**第几块**（它自己的块号，从 0 起）。
 *
 * ★ 这里必须是**块号**而不是全局下标：接下来要走的是「取回」那条路
 *   （`/api/query/files` 的 `block_indices` 就是“这份文件的第几块”）。
 *   上一版用 `disagg` 才需要全局下标，而那条路跨文件根本走不通。
 */
const crossBlocks = computed(() => {
  const c = crossCard.value
  if (!c || crossFileIdx.value === null) return []
  return cardBlocksPerFile(c)[crossFileIdx.value] || []
})

/**
 * 开「分解（跨文件）」弹框 —— 与旁边那个「分解（单文件）」是**两件事**：
 *
 * * 单文件：在那一份文件的向量上拖一个区间，拆出子集；
 * * 跨文件：这张卡横跨多份文件，选**其中一份**，把它在那张卡里占的那些下标拆出来。
 *   （以前勾中跨文件的卡只能被“请先按单份文件取一份”赶走，而现在把它拆开就行。）
 */
function openCrossDisagg() {
  const many = pool.selectedCards.filter((c) => (c.indices?.length || 0) >= 2)
  const cross = many.find((c) => cardFiles(c).length > 1)
  if (!cross) {
    ElMessage.warning(
      '先勾选一张跨文件的卡（只覆盖一份文件的那种，请用旁边那个「分解（单文件）」）',
    )
    return
  }
  crossCardId.value = cross.id
  crossFileIdx.value = null
  crossOpen.value = true
}

async function confirmCrossDisagg() {
  const c = crossCard.value
  if (!c) return
  if (crossFileIdx.value === null) {
    ElMessage.warning('先选一份文件')
    return
  }
  const fs = c.files || c.result?.files || []
  const f = fs[crossFileIdx.value]
  if (!f) {
    ElMessage.warning('这份文件已经不在这张卡里了')
    return
  }
  const blocks = crossBlocks.value
  if (!blocks.length) {
    ElMessage.warning('该卡片不包含所选文件的任何块。请重新选择。')
    return
  }
  // ★ 与「入池」同口径的闸门（审计 N6）：`blocks` 是这张卡在该文件区间内的
  //   下标，正常构造下它是卡指数的子集、撞不上；但卡的来源一旦变多就会先炸。
  if (blocks.length > MAX_QUERY_INDICES) {
    ElMessage.warning(
      `这份文件在这张卡里有 ${blocks.length} 块，超过一次能查的 ${MAX_QUERY_INDICES} 块 —— ` +
        '先把它拆成几张小卡，再逐张拆。',
    )
    return
  }
  crossRunning.value = true
  try {
    // ★★ 走**取回**那条路（`/api/query/files`），不是纯代数拆：
    //    targets 里**只有选中的那一份文件**，块号是它自己的第几块。
    //    拿到的是**属于这份文件自己的**证据 —— 能独立验证、能再聚合。
    //
    //   ★ 为什么不用 `disagg`：它要求“这批下标只属于一份文件”（为了拿唯一一份
    //     文件的素数视图算 crs_n），跨文件的卡走不通；而且它拆出来的东西属于
    //     **合并向量**，不是任何单份文件的。
    //
    //   ★★ 为什么 targets 必须取弹框里选的那一份：上一版出过的错就是它跑了
    //      “顶部文件框里选中的那个” —— 拿 50KB+64KB 的卡去拆，结果取回了
    //      `test_20KB`。所以这里**只看 `f`**，不碰 `fetchFileIds`。
    const { data } = await evidenceApi.queryFiles([[f.owner, f.file_key]], blocks)
    pool.addCard({
      label: `拆出（跨文件）：${f.file_key} 第 ${span(blocks)} 块`,
      src: '分解而来（跨文件 → 按文件取回）',
      result: data,
    })
    crossOpen.value = false
    ElMessage.success(
      `已从这张跨文件卡里拆出 ${f.file_key} 的第 ${span(blocks)} 块（共 ${blocks.length} 块）——` +
        '这是一份属于该文件自己的证据',
    )
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '拆出失败')
  } finally {
    crossRunning.value = false
  }
}

/**
 * 卡片**分两组**：单文件 与 跨文件（用户反馈第 4 条）。
 *
 * ★ 为什么要分开，而不是混在一个列表里：这两类卡的**性质不同** ——
 *
 *   * **单文件**：卡上就是那一份文件自己的向量承诺。要在它上面聚合，走的是
 *     同一个向量内的 ``VC.Agg``（只收凭证，不碰密文）；
 *   * **跨文件**：后端走 ``_prove_merged`` —— 先把各文件的承诺**抬进一条合并向量**
 *     再开证据。它的 ``indices`` 是**那条合并向量上的**下标，不是任何单份文件的下标；
 *     分解时也**不能**直接按单文件块号去切（那是另一套坐标）。
 *
 *   混在一起时，"这张卡到底在说谁"要靠逐张读标题才看得出来；分开之后一眼分明。
 *   组内保持原有顺序（不重排，免得刚取的那张突然换位置）。
 */
const orderedCards = computed(() => {
  const single = []
  const cross = []
  for (const c of pool.cards) {
    if (cardFiles(c).length > 1 || c.mergedUniverse) cross.push(c)
    else single.push(c)
  }
  return [...single, ...cross]
})

/** 某一组的第一张卡的位置（用于在它前面插图组标题）。 */
const groupBoundary = computed(() => {
  const cards = orderedCards.value
  const firstCross = cards.findIndex((c) => cardFiles(c).length > 1 || c.mergedUniverse)
  return { firstCross, total: cards.length }
})

/**
 * 第 ``i`` 张卡之前要不要插一个组标题。返回标题对象（不要插就返回 null）。
 *
 * ★ 只在「本组第一张」前面插 —— 用下标比较而不是给每张卡算一个「组 id」，
 *   是为了让模板里只有一层 ``v-for``（卡片那段结构很长，复制两份迟早会分叉）。
 */
function groupHeadAt(i) {
  const { firstCross } = groupBoundary.value
  if (i === 0) {
    return firstCross === 0
      ? {
          title: '跨文件证据（合并向量）',
          note:
            '由多份文件的承诺抬进一条合并向量后开的证据。它的位置属于那条合并向量，' +
            '不按单份文件编号 —— 分解时也不能按单文件块号去切。',
        }
      : {
          title: '单文件证据',
          note: '只覆盖一份文件，聚合走同一向量内的 VC.Agg（只收凭证，不碰密文）。',
        }
  }
  if (i === firstCross && firstCross > 0) {
    return {
      title: '跨文件证据（合并向量）',
      note:
        '由多份文件的承诺抬进一条合并向量后开的证据。它的位置属于那条合并向量，' +
        '不按单份文件编号 —— 分解时也不能按单文件块号去切。',
    }
  }
  return null
}

/** 这张卡是不是只覆盖一份文件（分解的硬要求）。 */
const disaggFiles = computed(() => cardFiles(disaggCard.value))
const disaggSingleFile = computed(() => disaggFiles.value.length <= 1)

/**
 * 这张卡还在当前 δ 上吗（分解的另一个硬要求）。
 *
 * ★ 与卡片列表上那个「已作废，需重新取」标签用的是**同一个判据**
 *   （`pool.staleIds`，按 δ 指纹）—— 只是这里针对弹窗里选中的那一张。
 *   作废的卡拆出来 100% 验不过，所以按钮直接禁掉，并给一颗「重新取一次」。
 */
const disaggCardFresh = computed(
  () => !!disaggCard.value && !pool.staleIds.has(disaggCard.value.id),
)

/**
 * 重新取一次某张卡（同一个下标集合，走 /api/query）。
 *
 * ★ `addCard` 对「下标集合一模一样的卡」是**覆盖**而不是新增，
 *   所以取完这张卡的 `fp` 就跟着当前 δ 走了，作废标记自己消失。
 */
const refetching = ref(false)
async function refetchCard(c) {
  if (!c) return
  // ★★ 与「入池」「集合验证」同口径的闸门（审计 N6）：卡的指数虽然受上游约束
  //   （入池已分片、集合验证有闸门），但这里是**最后一次**发给 /api/query 的
  //   地方 —— 超了后端只会回一句 422。提前拦住，并说清怎么办。
  if ((c.indices?.length || 0) > MAX_QUERY_INDICES) {
    ElMessage.warning(
      `这张卡覆盖 ${c.indices.length} 块，超过一次能查的 ${MAX_QUERY_INDICES} 块 —— ` +
        '请回「文件与块」重新入池（那边会按上限自动切成几张卡）。',
    )
    return
  }
  refetching.value = true
  try {
    const { data } = await evidenceApi.query(c.indices)
    pool.addCard({ label: `重取：${span(c.indices)}`, result: data, src: '重新取回' })
    ElMessage.success(`已按当前 δ 重新取回 ${c.indices.length} 块`)
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '重新取回失败')
  } finally {
    refetching.value = false
  }
}

/**
 * 打开分解弹窗。
 *
 * ★ 两条硬约束得先查清，否则用户填完范围才捶 400/500：
 *   ① 那张卡要覆盖 ≥ 2 个下标（否则拆不出真子集）；
 *   ② 那张卡必须**只覆盖一份文件** —— 证据是"这份文件的 n"的函数，
 *      跨文件的卡拆出来的 π_K 没有意义（后端会明确回 400）。
 *      原来只查了 ①，所以勾中跨文件的卡一点「分解」必然报错。
 */
function openDisagg() {
  const cards = pool.selectedCards
  if (!cards.length) {
    ElMessage.warning('先勾选一张卡片（覆盖至少 2 块）')
    return
  }
  const many = cards.filter((x) => (x.indices?.length || 0) >= 2)
  if (!many.length) {
    ElMessage.warning('勾中的卡片都只覆盖 1 块，拆不出真子集')
    return
  }
  const single = many.find((x) => cardFiles(x).length <= 1)
  if (!single) {
    const names = cardFiles(many[0])
    ElMessage.warning(
      '这个「分解（单文件）」要在同一份文件的卡上做 —— 勾中的卡片都跨了多份文件' +
        (names.length ? `（如 ${names.slice(0, 3).join('、')}${names.length > 3 ? ' 等' : ''}）` : '') +
        '。想按文件拆的话，请用旁边那个「分解（跨文件）」。',
    )
    return
  }
  disaggCardId.value = single.id
  resetDisaggRange()
  showAllIndices.value = false
  disaggOpen.value = true
}

/** 默认范围与旧行为一致：从第一个到**倒数第二个**（等于丢掉最后一块）。 */
function resetDisaggRange() {
  const idx = disaggIndices.value
  if (!idx.length) return
  disaggFrom.value = idx[0]
  disaggTo.value = idx.length >= 2 ? idx[idx.length - 2] : idx[0]
}

async function confirmDisagg() {
  const c = disaggCard.value
  if (!c) return
  const K = disaggK.value
  if (!K.length) {
    ElMessage.warning('选中的区间是空的')
    return
  }
  disaggRunning.value = true
  disaggResult.value = null
  try {
    const { data } = await evidenceApi.disagg({
      I: c.indices,
      values: c.result.values,
      S_I: c.result.proof.S_I,
      Lambda_I: c.result.proof.Lambda_I,
      K,
    })
    disaggResult.value = data
    pool.addCard({ label: `拆出：${span(K)}`, src: '分解而来', result: data })
    disaggOpen.value = false
    ElMessage.success(`已拆出覆盖 ${K.length} 块的证据`)
  } catch (e) {
    disaggResult.value = { ok: false, message: e?.response?.data?.detail || '分解失败' }
  } finally {
    disaggRunning.value = false
  }
}

onMounted(() => {
  syncDelta()
})
</script>

<template>
  <div>
    <PageHeader title="证据池" subtitle="取证据，也能跨文件聚合成一份" />

    <div class="panel mb-3">
      <div class="toolbar">
        <el-button :disabled="!pool.selectedCards.length" :loading="aggregating" @click="aggregateCards">聚合选中</el-button>
        <el-button :disabled="!pool.selectedCards.length" :loading="batchRunning" @click="batchVerify">一次验这 {{ pool.selectedCards.length }} 份</el-button>
        <el-button :disabled="!pool.selectedCards.length" @click="openDisagg" :loading="disaggRunning">分解（单文件）</el-button>
        <el-button :disabled="!pool.selectedCards.length" @click="openCrossDisagg" :loading="crossRunning">分解（跨文件）</el-button>
        <el-button v-if="pool.cards.length" link type="danger" @click="pool.clear()">清空</el-button>
        <span class="toolbar-right"><DetailToggle /></span>
      </div>
      <p class="note">
        只存在当前标签页（sessionStorage），关掉就没了。是否作废看 δ 指纹。
        ｜「聚合选中」在同一份文件内走论文的 VC.Agg（只收凭证，很快）；
        <b>跨文件时它做的是「归约」</b>：把各文件的承诺抬进一条合并向量、
        再用全量值重开一份证据（要取密文，秒级）——
        两条路的产物一样：两个群元素（256 字节），之后仍可再聚合、可分解。
        ｜要把某一份文件单独取一份证据，到「文件与块」页点它那行的「入池」
        （或右上角的「一键入池」）。
      </p>
    </div>

    <div v-if="!pool.cards.length">
      <EmptyState title="证据池为空" description="可取自取证结果，或从「完整性验证」页加入验证结果" icon="list" />
    </div>

    <div v-else class="cards">
      <!-- ★ 单文件 / 跨文件分开（用户反馈第 4 条）。
           两者的性质不同：单文件卡上是那一份文件自己的向量承诺；跨文件卡是
           `_prove_merged` 把各文件承诺抬进一条**合并向量**后开的，它的下标
           属于那条合并向量，不是任何单份文件的下标。混在一起看不出“这张卡在说谁”。
           只为让模板里只有一层 v-for（卡片那段结构很长，复制两份迟早分叉），
           分组标题在渲染时按位置插（见 `groupHeadAt`）。 -->
      <template v-for="(card, i) in orderedCards" :key="card.id">
        <div v-if="groupHeadAt(i)" class="card-group-head">
          <h5 class="group-title">{{ groupHeadAt(i).title }}</h5>
          <p class="group-note">{{ groupHeadAt(i).note }}</p>
        </div>
        <div class="card panel" :class="{ stale: isStale(card) }">
        <el-checkbox :model-value="pool.selectedIds.includes(card.id)" @change="pool.toggle(card.id)">
          <!-- ★ 挂 `title`：标题被省略号截断后，hover 还能看到全称。 -->
          <span class="card-title mono" :title="cardTitle(card)">{{ cardTitle(card) }}</span>
        </el-checkbox>
        <!-- ★ “看完整标题”：标题在卡上被省略号截断了（否则长标题会撞到右边那张卡），
             但它本身有信息量 —— 哪几份文件、各第几块。原生 `title` 只在悬停时
             出现（触屏拿不到、也不好复制），所以给一个点得开的入口。
             ★ 紧跟在标题下面：标题看不全 → 点开看全，是两个相邻的动作，
               中间不该隔着「取回于 …」。 -->
        <div style="margin: 2px 0 4px">
          <el-button link type="primary" size="small" @click="showFullTitle(card)">
            看完整标题
          </el-button>
        </div>
        <div class="card-meta">
          <span class="mono text-3">取回于 n={{ card.n ?? '—' }}</span>
          <span class="mono text-3">{{ fmtAgo(card.ts) }}</span>
          <!-- 来源必须显示：它决定了这张卡是「同文件内 VC.Agg」还是「跨文件归约」在的 -->
          <span v-if="card.src" class="text-3">{{ card.src }}</span>
        </div>
        <div v-if="card.result?.verify">
          <VerifyResult :result="card.result" />
        </div>

        <!-- 逐块分量：详细模式默认展开，单张卡片也能自己开合 -->
        <div v-if="card.indices?.length" class="card-fp">
          <div class="fp-head">
            <el-button link type="primary" size="small" @click="toggleCard(card)">
              {{ isOpen(card) ? '收起逐块指纹' : `细看逐块指纹（${card.indices.length} 块）` }}
            </el-button>
            <span v-if="card.delta_fp" class="text-3" style="font-size: 11px">δ 指纹 {{ card.delta_fp }}</span>
          </div>
          <BlockFingerprints
            v-if="isOpen(card)"
            :indices="card.indices"
            :values="card.result?.values || []"
            :refs="fpRefs(card)"
            :len="24"
          />
        </div>
        <el-tag v-if="isStale(card)" type="danger" size="small" class="mt-2">已作废，需重新取</el-tag>
        <div class="card-actions mt-2">
          <el-button link type="danger" size="small" @click="pool.removeCard(card.id)">移除</el-button>
        </div>
        </div>
      </template>
    </div>

    <!-- 分解（跨文件）：选**一份文件**，把它在这张卡里占的那些下标拆出来。
         ★ 与旁边那个「分解（单文件）」是两件事（见 openCrossDisagg 的注释）。
         弹框挂在 body 上，所以放在模板哪个位置都不影响渲染。 -->
    <el-dialog v-model="crossOpen" title="分解（跨文件）—— 选一份文件" width="600px">
      <p class="note">
        从这张跨文件的卡里，把「某一份文件」的那一部分「单独取回来」，
        得到一份属于它自己的、可以独立验证的证据。
        （跨文件的卡本身是在一条**合并向量**上开的，所以不能直接按单文件块号去拆；
        这里是按你选的那一份重新向节点取一次。）
      </p>
      <el-select v-model="crossFileIdx" placeholder="选要拆出哪一份文件" style="width: 100%">
        <el-option
          v-for="f in crossFileList"
          :key="f.i"
          :value="f.i"
          :label="`${f.file_key}（${f.owner}）—— 这张卡里有它的 ${f.blocks} 块`"
        />
      </el-select>
      <div v-if="crossFileIdx !== null" class="dg-row mt-2">
        <span class="dg-lbl">拆出</span>
        <span class="mono">{{ crossFileList[crossFileIdx]?.file_key }} 第 {{ span(crossBlocks) }} 块（{{ crossBlocks.length }} 块）</span>
      </div>
      <template #footer>
        <el-button @click="crossOpen = false">取消</el-button>
        <el-button type="primary" :loading="crossRunning" @click="confirmCrossDisagg">拆出</el-button>
      </template>
    </el-dialog>

    <div v-if="batchResult" class="panel mt-3">
      <h4 class="sec-title">批量验证结果</h4>
      <!-- ★ 可信级必须标出来（安全审计 S2）：证据池这条路的结论**全部来自服务端**
           （`/api/verify` / `/api/query`），客户端**没有**独立复算 —— 它和详情页
           那句「浏览器本地验证通过」**不是一个等级**。所以这里刻意做两件事：
           ① 不用 `success` 那套绿色（把两种可信级在视觉上分开）；
           ② 标题里把「服务端结论」写明，不让它冒充本地验证。
           等到 `/api/query` 也接上本地复算，这行标注才可以去掉。 -->
      <el-alert
        :type="batchResult.ok ? 'warning' : 'error'"
        :closable="false"
        :title="
          (batchResult.message || (batchResult.ok ? '通过' : '失败')) +
          '（服务端结论，未经本地验证）'
        "
      />
      <div v-if="batchResult.ms_batch != null" class="mono mt-2" style="font-size: 13px">
        <span>批量 {{ batchResult.ms_batch }} ms</span>
        <span class="sep">·</span>
        <span>逐份 {{ batchResult.ms_separate }} ms</span>
      </div>
      <p v-if="batchResult.ms_batch != null" class="note">批量验证比逐份慢（实测约 1.68 倍），换来的是一次结论。</p>
      <!-- 成功时把“这是在哪种向量上给的结论”说清：一文件一向量 vs 合并向量 -->
      <p v-if="batchResult.universe_files?.length" class="note">
        结论是在 <b>{{ batchResult.universe_files.join(' + ') }}</b> 这条向量上做的
        （{{ batchResult.universe_n }} 个位置<template v-if="batchResult.merged_universe">，由多份文件归约而来</template>）。
      </p>
      <!--
        ★ 只有“卡来自不同宇宙”时后端才拒绝。这必须给出可执行的下一步，因为它不是故障：
          一次结论要求所有份共享同一个承诺 C（学位论文 Def. 29），而每张纯单文件的卡
          各绑在自己那份文件的 C_f 上。
          注意：一张“归约”出来的跨文件卡本身**可以**一次结论（它已经落在一条向量上）。
      -->
      <template v-if="batchResult.needsMerge">
        <p class="note">
          一次结论要求所有份共享同一个承诺 C（论文 Def. 29），而这几张卡各绑在自己那份文件的 C 与 U 上
          —— 所以没法合成一条方程。注意：一张「聚合选中」归约出来的跨文件卡
          「可以」一次结论（它已经落在一条向量上）；要一次验好几张不同的卡，
          先用「聚合选中」把它们并成一张，再验那一张。
        </p>
        <el-button type="primary" size="small" :loading="aggregating" @click="mergeNow">
          改用聚合选中（并成一条向量）
        </el-button>
      </template>
      <el-alert v-if="batchResult.agree === false" type="error" :closable="false" class="mt-2" title="两套结论不一致（这通常意味着实现有 bug）" />
      <StageTimeline v-if="batchResult.timings" :timings="batchResult.timings" class="mt-2" />
    </div>

    <div v-if="disaggResult" class="panel mt-3">
      <h4 class="sec-title">分解结果</h4>
      <div class="text-2" style="font-size: 13px">
        从 {{ blockText(disaggCard, disaggResult.source_indices || []) }}
        拆出 {{ blockText(disaggCard, disaggResult.indices || []) }}，
        丢弃 {{ blockText(disaggCard, disaggResult.dropped || []) }}
      </div>
      <p class="note">第 3 步不能省：要拆出新块，得先把它取回来。</p>
    </div>

    <!--
      分解：选「从哪分到哪」。
      ★ 下标太多时**不铺开也能看全**：默认给压缩区间（如 0-1023），
        展开后是一个限高可滚动的框，不会把对话框撞破。
      ★ 范围用下拉而不是手填数字：选出来的 K 必然 ⊆ I，不会白撞一次 400。
    -->
    <el-dialog v-model="disaggOpen" title="分解证据（选从哪分到哪）" width="560px">
      <template v-if="disaggCard">
        <div v-if="pool.selectedCards.length > 1" class="dg-row">
          <span class="dg-lbl">哪张卡</span>
          <el-select
            v-model="disaggCardId"
            style="flex: 1; min-width: 240px"
            @change="resetDisaggRange"
          >
            <el-option
              v-for="c in pool.selectedCards"
              :key="c.id"
              :value="c.id"
              :label="cardTitle(c)"
            />
          </el-select>
        </div>

        <div class="dg-row">
          <span class="dg-lbl">这张卡覆盖</span>
          <span class="mono">{{ disaggIndices.length }} 块</span>
          <el-button link type="primary" size="small" @click="showAllIndices = !showAllIndices">
            {{ showAllIndices ? '收起完整块号' : '展开完整块号' }}
          </el-button>
        </div>
        <!-- ★ 说清"这是哪份文件的证据"：分解的硬约束就是它必须只有一份文件。 -->
        <div v-if="disaggFiles.length" class="dg-row">
          <span class="dg-lbl">所属文件</span>
          <span class="mono" :class="{ 'text-danger': !disaggSingleFile }">
            {{ disaggFiles.join('、') }}
          </span>
          <span v-if="!disaggSingleFile" class="text-danger">
            —— 跨了 {{ disaggFiles.length }} 份文件，不能分解；请按单份文件重新取一份证据
          </span>
        </div>
        <!--
          ★ 分解这条路**一个网络请求都不发**：它只是在旧证据上做代数。
          所以「这张卡是不是还在当前 δ 上」就是它唯一的失败原因 ——
          作废的卡拆出来必然验不过（那是 S_I^{e_K} ≠ U_n，报错看不懂）。
          与其让人选完区间才被弹一句密码学错误，不如在这里先说清、并配一颗按钮。
        -->
        <div v-if="!disaggCardFresh" class="dg-row">
          <span class="dg-lbl">这张卡</span>
          <span class="text-danger">
            已作废：是在 n={{ disaggCard.n ?? '—' }} 上取的，当前是 n={{ pool.currentN ?? '—' }}
            —— 拆出来的证据必然验不过
          </span>
          <el-button
            link
            type="primary"
            size="small"
            :loading="refetching"
            @click="refetchCard(disaggCard)"
          >
            重新取一次
          </el-button>
        </div>
        <div class="dg-idx" :class="{ open: showAllIndices }">
          <span class="mono">{{ blockText(disaggCard, disaggIndices, showAllIndices) }}</span>
        </div>
        <p class="note">
          界面上只说「哪份文件的第几块」—— 全局下标是内部坐标，不摆出来。
        </p>

        <div class="dg-row mt-2">
          <span class="dg-lbl">从哪分到哪</span>
          <el-select v-model="disaggFrom" style="width: 130px" filterable>
            <el-option v-for="i in disaggIndices" :key="i" :value="i" :label="idxLabel(i)" />
          </el-select>
          <span class="text-3">→</span>
          <el-select v-model="disaggTo" style="width: 130px" filterable>
            <el-option v-for="i in disaggIndices" :key="i" :value="i" :label="idxLabel(i)" />
          </el-select>
        </div>

        <div class="dg-preview">
          <div>拆出 π_K：<span class="mono">{{ blockText(disaggCard, disaggK) }}</span>（{{ disaggK.length }} 块）</div>
          <div>丢弃：<span class="mono">{{ blockText(disaggCard, disaggDropped) }}</span>（{{ disaggDropped.length }} 块）</div>
          <p v-if="!disaggDropped.length" class="dg-warn">
            丢弃为空 —— 这样等于原样复制一份，分解的意义在于「只留一部分」。把范围缩小一点。
          </p>
          <p v-else class="note">分解不发任何网络请求；含「新」块的证据得另外去取（第 3 步不能省）。</p>
        </div>
      </template>
      <template #footer>
        <el-button @click="disaggOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="disaggRunning"
          :disabled="!disaggK.length || !disaggSingleFile || !disaggCardFresh"
          @click="confirmDisagg"
        >分解</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}
.toolbar-right {
  margin-left: auto;
}
.card-fp {
  margin-top: 8px;
  border-top: 1px dashed var(--line);
  padding-top: 6px;
}
.fp-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
}
.fetch-input {
  width: 180px;
}
.note {
  font-size: 12px;
  color: var(--text-3);
  margin-top: 8px;
}
/* ★ 卡片标题**单行省略**（用户反馈）：跨文件卡的标题可以很长，例如
   「聚合：test_10KB 第 0-10 块；test_20KB 第 0-19 块；test_50KB 第 0-49 块」——
   不截断的话它会把 grid 的列**撑开**，压到右边相邻的那张卡上。

   ★★ 关键是给 grid / flex 的子项 `min-width: 0`：不加这一条，
   `text-overflow: ellipsis` **根本不生效** —— 子项的默认 `min-width` 是 `auto`，
   内容多宽它就要多宽，于是 ellipsis 永远轮不到上场。
   完整标题仍可在 hover 时看到（模板里挂了 `title`）。 */
.cards > .card {
  min-width: 0;
}
.card :deep(.el-checkbox) {
  display: flex;
  align-items: flex-start;
  min-width: 0;
}
.card :deep(.el-checkbox__label) {
  min-width: 0;
  overflow: hidden;
}
.card .card-title {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 分组标题（单文件 / 跨文件）—— 必须跨满整行：`.cards` 是 grid，
   不写 grid-column 的话标题会变成其中**一格**（宽 320px）挤在卡片之间。 */
.card-group-head {
  grid-column: 1 / -1;
  margin: 6px 0 0;
}
.group-title {
  font-size: 13px;
  font-weight: 600;
  margin: 0 0 4px;
}
.group-note {
  font-size: 12px;
  line-height: 1.6;
  color: var(--text-3);
  margin: 0 0 10px;
  max-width: 90ch;
}
.cards {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 12px;
}
.card.stale {
  opacity: 0.6;
}
.card-title {
  font-size: 13px;
  font-weight: 500;
}
.card-meta {
  display: flex;
  gap: 12px;
  margin: 4px 0 10px;
  font-size: 12px;
}
.sec-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 12px;
}
.sep {
  margin: 0 8px;
  color: var(--text-3);
}
/* ---------- 分解弹窗 ---------- */
.dg-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.dg-lbl {
  font-size: 13px;
  color: var(--text-2);
  min-width: 72px;
}
/* 限高 + 可滚动：块数多时展开也不会把对话框撞破。 */
.dg-idx {
  max-height: 96px;
  overflow: auto;
  border: 1px dashed var(--line);
  border-radius: var(--radius-sm);
  padding: 6px 8px;
  font-size: 12px;
  color: var(--text-2);
  word-break: break-all;
}
.dg-idx.open {
  max-height: 140px;
}
.dg-preview {
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--line);
  font-size: 13px;
}
.dg-warn {
  margin-top: 6px;
  font-size: 12px;
  color: var(--warn);
}
</style>
