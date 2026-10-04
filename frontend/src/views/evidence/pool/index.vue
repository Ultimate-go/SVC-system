<script setup>
/**
 * 证据池。
 *
 * - 池子存 sessionStorage（关掉标签页就没了，刻意）。
 * - 每张卡片记录 indices / values / proof / delta_fp / delta_n / 取回时间。
 * - 按 delta_fp 判断作废（不是 n）。
 * - 「一次验这 N 份」→ verify-batch，两个耗时都显示，agree 不一致报警。
 * - 分解再聚合 4 步演示。
 * - 故障演练：勾选卡片后，把要发出去的那一份副本改坏一个值再验。
 * - **逐块指纹**：每张卡片可以展开看"这份证据覆盖的每个下标，它的分量指纹是多少"
 *   —— 跟着全局的「简略/详细」开关，也能在单张卡片上自己开合。
 */
import { ref, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { usePoolStore } from '../../../stores/pool'
import { useThemeStore } from '../../../stores/theme'
import { evidenceApi } from '../../../api/evidence'
import { filesApi } from '../../../api/files'
import { systemApi } from '../../../api/system'
import { span, fmtAgo } from '../../../utils/format'
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

const corruptResult = ref(null)
const corruptRunning = ref(false)

/* ---- 取回：**选文件 + 第几块**（主交互）----
 *
 * ★ 为什么不再让用户填"全局下标"：那是**内部坐标**，它由上传顺序决定，
 *   和用户脑子里想的"这份文件的第几块"是两码事。把内部坐标露到界面上，
 *   用户就得自己去数前面的文件占了多少位置 —— 错一次还很难看出来。
 *   现在界面上只有文件名与块号，转换成全局下标是前端自己的事。
 */
const fileOptions = ref([])
const fetchFileId = ref(null)
const blockInput = ref('0')
/** 副本数（从 /api/status 读）—— 取回耗时的口径之一，读不到就不显示。 */
const replicas = ref(null)

const pickedFile = computed(
  () => fileOptions.value.find((f) => f.id === fetchFileId.value) || null,
)

/**
 * 这次取回大概要多久 —— 界面上必须有个数，但**绝不编毫秒**。
 *
 * ★ 口径来自方案本身：一次取回里，每块都要问**持它的那 r 台**节点各算
 *   一次模幂，所以耗时 ≈ 块数 × 副本数 × 单次模幂。前两项能算准，第三项
 *   取决于机器 —— 所以这里给"要打几台、节点要算几次"这种**可核对**的口径
 *   加一个相对档位；真正的毫秒数由响应里的 timings 带回来。
 */
const fetchEta = computed(() => {
  const f = pickedFile.value
  if (!f) return null
  let blocks
  try {
    blocks = parseBlockRange(blockInput.value, f.block_count)
  } catch {
    return null
  }
  if (!blocks.length) return null
  const level =
    blocks.length <= 2 ? '很快' : blocks.length <= 8 ? '要等一下' : '会明显等一会儿'
  return {
    blocks: blocks.length,
    replicas: typeof replicas.value === 'number' ? replicas.value : null,
    asks: typeof replicas.value === 'number' ? blocks.length * replicas.value : null,
    level,
  }
})

async function loadFiles() {
  try {
    const { data } = await filesApi.list()
    fileOptions.value = Array.isArray(data) ? data : []
    if (!fileOptions.value.some((f) => f.id === fetchFileId.value)) {
      fetchFileId.value = fileOptions.value[0]?.id ?? null
    }
  } catch {
    /* 列表拉不到就先空着，取回时会给提示 */
  }
}

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
      .map((f) => `${f.file_key} 第 ${span(f.block_indices || [])} 块`)
      .join('；')
  }
  return span(card.indices)
}

function cardTitle(card) {
  const scope = cardScope(card)
  const raw = String(card.label || '')
  if (!raw) return scope
  const cut = raw.search(/[：:]/)
  return `${cut >= 0 ? raw.slice(0, cut) : raw}：${scope}`
}

async function syncDelta() {
  try {
    const { data } = await systemApi.status()
    pool.syncDelta({ fp: null, n: data.delta.n })
    // 副本数：取回耗时的口径之一（每块要问持它的那 r 台）。
    // 拿不到就留 null —— 界面上宁可不显示这个数，也不要显示一个编的。
    replicas.value = data?.crs?.replicas ?? data?.replicas ?? null
    // 也取指纹：status 没有 delta_fp，用 query 一个空集取不到 —— 直接用 n 与已有卡比对。
    // 实际上 delta_fp 需要从某次 query 拿。这里从池子里已有的 fp 兜底。
  } catch {
    /* 忽略 */
  }
}

async function fetchOne() {
  const f = pickedFile.value
  if (!f) {
    ElMessage.warning('先选一份文件')
    return
  }
  let blocks
  try {
    blocks = parseBlockRange(blockInput.value, f.block_count)
  } catch (e) {
    ElMessage.error(e?.message || '块号写法不对')
    return
  }
  if (!blocks.length) {
    ElMessage.warning('请输入第几块')
    return
  }
  try {
    // ★ 界面坐标 → 内部坐标的**唯一转换点**：块号交给后端，由它按这份文件的
    //   位置段换成全局下标（前端不复现那份映射，免得多一处会分叉的真相）。
    //
    //   targets 的形状是 **[owner, file_key] 数组对**（后端是
    //   ``list[tuple[str, str]]``）—— 发成 ``{owner, file_key}`` 对象会 422，
    //   而且 422 的报错只说"字段不合法"，看不出是形状问题。
    const { data } = await evidenceApi.queryFiles(
      [[f.owner, f.file_key]],
      blocks,
    )
    pool.addCard({
      label: `取回：${f.file_key} 第 ${span(blocks)} 块`,
      src: '手动取回',
      result: data,
    })
    ElMessage.success(
      `已取回 ${f.file_key} 第 ${span(blocks)} 块（共 ${blocks.length} 块）的证据`,
    )
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '取回失败')
  }
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
    batchResult.value = { ok: false, message: e?.response?.data?.detail || '批量验证失败' }
  } finally {
    batchRunning.value = false
  }
}

async function corruptVerify() {
  const cards = pool.selectedCards
  if (!cards.length) {
    ElMessage.warning('先勾选一张卡片')
    return
  }
  const c = cards[0]
  const vals = c.result?.values
  if (!vals || !vals.length) {
    ElMessage.warning('这张卡没有可改坏的值')
    return
  }
  corruptRunning.value = true
  corruptResult.value = null
  try {
    // ★ 只改「要发出去的那一份副本」，不动池子、更不动服务器数据。
    const items = [
      {
        indices: c.indices,
        values: vals.map((v, i) => (i === 0 ? (BigInt(v) + 1n).toString() : v)),
        proof: c.result.proof,
      },
    ]
    const { data } = await evidenceApi.verifyBatch(items, true, false)
    corruptResult.value = data
  } catch (e) {
    corruptResult.value = { ok: false, message: e?.response?.data?.detail || '演练失败' }
  } finally {
    corruptRunning.value = false
  }
}

/** 这张卡覆盖的**文件**（去重后的 "owner/file_key" 列表）。 */
function cardFiles(card) {
  const fs = card?.files || card?.result?.files || []
  return [...new Set(fs.map((f) => `${f.owner} / ${f.file_key}`))]
}

/** 这张卡是不是只覆盖一份文件（分解的硬要求）。 */
const disaggFiles = computed(() => cardFiles(disaggCard.value))
const disaggSingleFile = computed(() => disaggFiles.value.length <= 1)

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
    ElMessage.warning('先勾选一张卡片（覆盖至少 2 个下标）')
    return
  }
  const many = cards.filter((x) => (x.indices?.length || 0) >= 2)
  if (!many.length) {
    ElMessage.warning('勾中的卡片都只覆盖 1 个下标，拆不出真子集')
    return
  }
  const single = many.find((x) => cardFiles(x).length <= 1)
  if (!single) {
    const names = cardFiles(many[0])
    ElMessage.warning(
      '分解要在「同一份文件」的卡上做 —— 勾中的卡片都跨了多份文件' +
        (names.length ? `（如 ${names.slice(0, 3).join('、')}${names.length > 3 ? ' 等' : ''}）` : '') +
        '。请先在「取回」里按单份文件取一份证据，再分解。',
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
    ElMessage.success(`已拆出覆盖 ${K.length} 个下标的证据`)
  } catch (e) {
    disaggResult.value = { ok: false, message: e?.response?.data?.detail || '分解失败' }
  } finally {
    disaggRunning.value = false
  }
}

onMounted(() => {
  syncDelta()
  loadFiles()
})
</script>

<template>
  <div>
    <PageHeader title="证据池" subtitle="取证据，也能跨文件聚合成一份" />

    <div class="panel mb-3">
      <div class="toolbar">
        <el-select
          v-model="fetchFileId"
          class="fetch-input"
          placeholder="选一份文件"
          filterable
        >
          <el-option
            v-for="f in fileOptions"
            :key="f.id"
            :value="f.id"
            :label="`${f.file_key}（${f.owner}，${f.block_count} 块）`"
          />
        </el-select>
        <el-input
          v-model="blockInput"
          class="fetch-input"
          placeholder="第几块，如 0 或 0-2"
          clearable
        />
        <el-button type="primary" @click="fetchOne">取回证据</el-button>
        <el-button :disabled="!pool.selectedCards.length" @click="pool.aggregateSelected()">聚合选中</el-button>
        <el-button :disabled="!pool.selectedCards.length" :loading="batchRunning" @click="batchVerify">一次验这 {{ pool.selectedCards.length }} 份</el-button>
        <el-button :disabled="!pool.selectedCards.length" @click="openDisagg" :loading="disaggRunning">分解</el-button>
        <el-button v-if="pool.cards.length" link type="danger" @click="pool.clear()">清空</el-button>
        <span class="toolbar-right"><DetailToggle /></span>
      </div>
      <p class="note">
        只存在当前标签页（sessionStorage），关掉就没了。是否作废看 δ 指纹。
        <template v-if="fetchEta">
          ｜本次取回 {{ fetchEta.blocks }} 块：
          <template v-if="fetchEta.asks">
            每块要问持它的那 {{ fetchEta.replicas }} 台，共 {{ fetchEta.asks }} 次模幂
          </template>
          <template v-else>块数越多越慢（每块都要问持它的那几台各算一次）</template>
          —— {{ fetchEta.level }}。
        </template>
      </p>
    </div>

    <div v-if="!pool.cards.length">
      <EmptyState title="池子是空的" description="取一份证据，或从「完整性验证」页把结果收进来" icon="list" />
    </div>

    <div v-else class="cards">
      <div v-for="card in pool.cards" :key="card.id" class="card panel" :class="{ stale: isStale(card) }">
        <el-checkbox :model-value="pool.selectedIds.includes(card.id)" @change="pool.toggle(card.id)">
          <span class="card-title mono">{{ cardTitle(card) }}</span>
        </el-checkbox>
        <div class="card-meta">
          <span class="mono text-3">取回于 n={{ card.n ?? '—' }}</span>
          <span class="mono text-3">{{ fmtAgo(card.ts) }}</span>
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
            :len="24"
          />
        </div>
        <el-tag v-if="isStale(card)" type="danger" size="small" class="mt-2">已作废，需重新取</el-tag>
        <div class="card-actions mt-2">
          <el-button link type="danger" size="small" @click="pool.removeCard(card.id)">移除</el-button>
        </div>
      </div>
    </div>

    <div v-if="batchResult" class="panel mt-3">
      <h4 class="sec-title">批量验证结果</h4>
      <el-alert
        :type="batchResult.ok ? 'success' : 'error'"
        :closable="false"
        :title="batchResult.message || (batchResult.ok ? '通过' : '失败')"
      />
      <div class="mono mt-2" style="font-size: 13px">
        <span>批量 {{ batchResult.ms_batch }} ms</span>
        <span class="sep">·</span>
        <span>逐份 {{ batchResult.ms_separate }} ms</span>
      </div>
      <p class="note">批量验证比逐份慢（实测约 1.68 倍），换来的是一次结论。</p>
      <el-alert v-if="batchResult.agree === false" type="error" :closable="false" class="mt-2" title="两套结论不一致（这通常意味着实现有 bug）" />
      <StageTimeline v-if="batchResult.timings" :timings="batchResult.timings" class="mt-2" />
    </div>

    <div class="panel mt-3">
      <h4 class="sec-title">故障演练</h4>
      <div class="flex items-center gap-3">
        <span class="text-2">把要发出去的那一份副本改坏一个值再验（不动池子，也不动服务器数据）</span>
        <el-button :disabled="!pool.selectedCards.length" :loading="corruptRunning" @click="corruptVerify">演练</el-button>
      </div>
      <div v-if="corruptResult" class="mt-2">
        <el-alert :type="corruptResult.ok ? 'error' : 'error'" :closable="false" :title="corruptResult.ok ? '异常：改坏了却仍然通过' : `被抓住：${corruptResult.code_name || corruptResult.message}`" />
      </div>
    </div>

    <div v-if="disaggResult" class="panel mt-3">
      <h4 class="sec-title">分解结果</h4>
      <div class="text-2" style="font-size: 13px">
        从 {{ span(disaggResult.source_indices || []) }} 拆出 {{ span(disaggResult.indices || []) }}，
        丢弃 {{ span(disaggResult.dropped || []) }}
      </div>
      <p class="note">第 3 步不能省：要拆出新下标，得先把它取回来。</p>
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
          <span class="mono">{{ disaggIndices.length }} 个下标</span>
          <el-button link type="primary" size="small" @click="showAllIndices = !showAllIndices">
            {{ showAllIndices ? '收起完整下标' : '展开完整下标' }}
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
        <div class="dg-idx" :class="{ open: showAllIndices }">
          <span class="mono">{{ showAllIndices ? disaggIndices.join(', ') : span(disaggIndices) }}</span>
        </div>

        <div class="dg-row mt-2">
          <span class="dg-lbl">从哪分到哪</span>
          <el-select v-model="disaggFrom" style="width: 130px" filterable>
            <el-option v-for="i in disaggIndices" :key="i" :value="i" :label="String(i)" />
          </el-select>
          <span class="text-3">→</span>
          <el-select v-model="disaggTo" style="width: 130px" filterable>
            <el-option v-for="i in disaggIndices" :key="i" :value="i" :label="String(i)" />
          </el-select>
        </div>

        <div class="dg-preview">
          <div>拆出 π_K：<span class="mono">{{ span(disaggK) }}</span>（{{ disaggK.length }} 块）</div>
          <div>丢弃：<span class="mono">{{ span(disaggDropped) }}</span>（{{ disaggDropped.length }} 块）</div>
          <p v-if="!disaggDropped.length" class="dg-warn">
            丢弃为空 —— 这样等于原样复制一份，分解的意义在于「只留一部分」。把范围缩小一点。
          </p>
          <p v-else class="note">分解不发任何网络请求；含「新」下标的证据得另外去取（第 3 步不能省）。</p>
        </div>
      </template>
      <template #footer>
        <el-button @click="disaggOpen = false">取消</el-button>
        <el-button
          type="primary"
          :loading="disaggRunning"
          :disabled="!disaggK.length || !disaggSingleFile"
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
