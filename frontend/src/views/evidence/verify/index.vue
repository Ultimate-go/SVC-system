<script setup>
/**
 * 完整性验证（验证不受限）。
 *
 * - 输入全局下标（支持 0-3, 8 区间语法）+ 允许部分结果开关。
 * - 展示两层结论（承诺层 / 块哈希层）、code_name、proof.size_bytes、holders、refs、missing。
 * - 「按文件查」：**所有者 / 文件标识两个条件各自可以单独用** ——
 *   只填所有者 = 查他名下所有文件；只填文件标识 = 查所有叫这个名字的文件；
 *   两个都填 = 精确到那一个。命中的文件一起取回，天然合成**一份**证据。
 * - 「按全局下标查登记表」。
 * - **逐块指纹**：验证结果里可以看到每个下标对应的分量
 *   —— 跟着全局的「简略 / 详细」开关（详细模式才铺出来）。
 */
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { evidenceApi } from '../../../api/evidence'
import { filesApi } from '../../../api/files'
import { usePoolStore } from '../../../stores/pool'
import { useThemeStore } from '../../../stores/theme'
import { span } from '../../../utils/format'
import { parseIndexRange } from '../../../utils/validate'
import PageHeader from '../../../components/common/PageHeader.vue'
import DetailToggle from '../../../components/common/DetailToggle.vue'
import VerifyResult from '../../../components/security/VerifyResult.vue'
import BlockFingerprints from '../../../components/security/BlockFingerprints.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'

const pool = usePoolStore()
const theme = useThemeStore()
/** 详细模式：把逐块分量铺出来（简略模式只给结论）。 */
const detailed = computed(() => theme.detailMode === 'detail')

const indicesInput = ref('0-3, 8')
const allowPartial = ref(false)
const running = ref(false)
const result = ref(null)

const fileOwner = ref('')
const fileKey = ref('')
//: ① 筛选：只把文件**列出来** —— 不取证据、不验证
const fileListingRunning = ref(false)
const fileListed = ref(false)
const fileMatches = ref([])
const fileQueryError = ref('')
//: ② 用户自己勾了哪几个（存文件 id）
const pickedIds = ref([])
//: ③ 对勾中的那几个取一份证据并验证
const fileVerifyRunning = ref(false)
const fileQueryResult = ref(null)

const regIndex = ref('')
const regResult = ref(null)
const regRunning = ref(false)

async function runQuery() {
  let indices
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
  running.value = true
  result.value = null
  try {
    const { data } = await evidenceApi.query(indices, allowPartial.value)
    result.value = data
    // 收进证据池
    pool.addCard({ label: `查询：${indices.join(',')}`, src: '完整性验证', result: data })
  } catch (e) {
    result.value = { ok: false, verify: { ok: false, code_name: '', message: e?.response?.data?.detail || '查询失败' } }
  } finally {
    running.value = false
  }
}

/**
 * 按文件查 —— **先查出来，再由用户挑哪几个去验**。
 *
 * 三步分开，关键是把「列出文件」和「取证据 + 验证」拆开：
 *
 *   ① `listFiles()`  只 `GET /api/files` 筛一遍，把命中的文件**列出来**。
 *      —— 不发任何取数据的请求，所以再多的文件也是快的；
 *         也**不会**自动验证（用户明确要的是自己挑）。
 *   ② 用户在列表里勾选（`togglePick` / `pickAll`），勾了几个就是几个。
 *   ③ `verifyPicked()` 才把勾中的 `(owner, file_key)` 交给 `/api/query/files`
 *      —— 设计 B 下它们天然聚合成**一份**证据，一次验证。
 *
 * 为什么拆开很值：原来点「查询」就把命中的所有文件全取一遍并验证，
 * 实测「只填所有者 zhangsan」命中 888 块要 18.6 秒，用户还没得选。
 * 现在不勾就不取，代价只跟**你真正要看的那几个**成正比。
 *
 * 两个条件各自可以单独用：只填所有者 = 他名下全部文件；只填文件标识 = 所有
 * 叫这个名字的文件（可能跨用户）；两个都填 = 精确一个。
 */
async function listFiles() {
  const owner = fileOwner.value.trim()
  const key = fileKey.value.trim()
  if (!owner && !key) {
    ElMessage.warning('至少填一个条件：所有者 或 文件标识')
    return
  }
  fileListingRunning.value = true
  fileListed.value = false
  fileMatches.value = []
  pickedIds.value = []
  fileQueryResult.value = null
  fileQueryError.value = ''
  try {
    const { data: all } = await filesApi.list()
    const list = Array.isArray(all) ? all : (all.items || [])

    // 两个条件各自可选：填了才参与筛选
    const hit = list.filter(
      (f) => (!owner || f.owner === owner) && (!key || f.file_key === key),
    )
    fileListed.value = true
    if (!hit.length) {
      fileQueryError.value =
        `没有匹配的文件（所有者：${owner || '不限'}，文件标识：${key || '不限'}）`
      return
    }

    fileMatches.value = hit.map((f) => ({
      id: f.id,
      owner: f.owner,
      file_key: f.file_key,
      block_count: f.block_count,
      indices: f.indices || [],
    }))
    // ★ 故意不默认勾选：让用户自己决定验哪几个
  } catch (e) {
    fileQueryError.value = e?.response?.data?.detail || e?.message || '查询失败'
  } finally {
    fileListingRunning.value = false
  }
}

function togglePick(id) {
  const i = pickedIds.value.indexOf(id)
  if (i >= 0) pickedIds.value.splice(i, 1)
  else pickedIds.value.push(id)
}

function pickAll() {
  pickedIds.value = fileMatches.value.map((f) => f.id)
}

function clearPicks() {
  pickedIds.value = []
}

/** 勾中的文件总共要取多少块 —— 验证前先让用户知道这次的分量。 */
const pickedBlocks = computed(() =>
  fileMatches.value
    .filter((f) => pickedIds.value.includes(f.id))
    .reduce((n, f) => n + (f.block_count || 0), 0),
)

/** 勾中的那些文件 —— 「当前文件与块数」那一块用。 */
const pickedFiles = computed(() =>
  fileMatches.value.filter((f) => pickedIds.value.includes(f.id)),
)

/**
 * 「当前文件与块数」（按全局下标验证那边）。
 *
 * ★ 一个证据可以横跨多个文件（设计 B），所以这是**真列表**而不是单个值：
 *   逐文件统计这份证据盖住了它几块。数据源是响应里的 refs
 *   （每个全局下标 → 哪个文件的第几块），拿不到归属时如实说“没有归属信息”。
 */
const resultFiles = computed(() => {
  const map = new Map()
  for (const r of result.value?.refs || []) {
    const name = `${r.owner} / ${r.file_key}`
    map.set(name, (map.get(name) || 0) + 1)
  }
  return [...map.entries()].map(([name, blocks]) => ({ name, blocks }))
})

/** 勾中的这些取一份证据并验证（**只有这一步会真的去取数据**）。 */
async function verifyPicked() {
  const picked = fileMatches.value.filter((f) => pickedIds.value.includes(f.id))
  if (!picked.length) {
    ElMessage.warning('先勾选要验证的文件')
    return
  }
  fileVerifyRunning.value = true
  fileQueryResult.value = null
  fileQueryError.value = ''
  try {
    const { data } = await evidenceApi.queryFiles(
      picked.map((f) => [f.owner, f.file_key]),
      null,
    )
    fileQueryResult.value = data
    const label =
      picked.length === 1
        ? `${picked[0].owner}/${picked[0].file_key}`
        : `选中的 ${picked.length} 个文件`
    pool.addCard({ label, src: '按文件查', result: data })
  } catch (e) {
    fileQueryError.value = e?.response?.data?.detail || e?.message || '验证失败'
  } finally {
    fileVerifyRunning.value = false
  }
}

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
      <h4 class="sec-title">按全局下标验证</h4>
      <div class="query-form">
        <el-input v-model="indicesInput" placeholder="如 0-3, 8" style="width: 260px" @keyup.enter="runQuery" />
        <el-switch v-model="allowPartial" active-text="允许部分结果" />
        <el-button type="primary" :loading="running" @click="runQuery">验证</el-button>
      </div>

      <div v-if="result" class="mt-3">
        <VerifyResult :result="result" />
        <!-- 当前文件与块数：一份证据可以横跨多个文件（设计 B），所以逐文件列 -->
        <div class="cur-file">
          <span class="cur-lbl">当前文件与块数</span>
          <template v-if="resultFiles.length">
            <span v-for="f in resultFiles" :key="f.name" class="cur-item mono">
              {{ f.name }}<span class="text-3"> · 本次覆盖 {{ f.blocks }} 块</span>
            </span>
          </template>
          <span v-else class="cur-item text-3">这份证据没有文件归属信息（只按全局下标取的）</span>
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

    <div class="panel mb-3">
      <h4 class="sec-title">按文件查（先查出来，再勾选要验的）</h4>
      <div class="query-form">
        <el-input
          v-model="fileOwner"
          placeholder="所有者（可只填这个）"
          style="width: 200px"
          @keyup.enter="listFiles"
        />
        <el-input
          v-model="fileKey"
          placeholder="文件标识（可只填这个）"
          style="width: 200px"
          @keyup.enter="listFiles"
        />
        <el-button type="primary" :loading="fileListingRunning" @click="listFiles">查询</el-button>
      </div>
      <p class="note">
        只填所有者：查他名下全部文件；只填文件标识：查所有叫这个名字的文件（可能跨用户）；
        两个都填：只查那一份。<strong>查询只列出文件，不取证据、不验证</strong>；
        勾选后再点下面的按钮，才把选中的一起取回来验证。
      </p>

      <div v-if="fileQueryError" class="file-error">{{ fileQueryError }}</div>

      <template v-if="fileMatches.length">
        <div class="match-head">
          <span class="text-2" style="font-size: 12px">
            匹配 {{ fileMatches.length }} 个文件，已勾选 {{ pickedIds.length }} 个
          </span>
          <el-button link type="primary" size="small" @click="pickAll">全选</el-button>
          <el-button
            v-if="pickedIds.length"
            link
            type="danger"
            size="small"
            @click="clearPicks"
          >清空选择</el-button>
        </div>

        <!-- 当前文件与块数：勾中的每一个 + 合计（未勾时整块不出现） -->
        <div v-if="pickedFiles.length" class="cur-file">
          <span class="cur-lbl">当前文件与块数</span>
          <span v-for="f in pickedFiles" :key="f.id" class="cur-item mono">
            {{ f.owner }} / {{ f.file_key }}<span class="text-3"> · {{ f.block_count }} 块</span>
          </span>
          <span class="cur-total mono">合计 {{ pickedBlocks }} 块</span>
        </div>

        <div class="match-list">
          <div v-for="m in fileMatches" :key="m.id" class="match-row">
            <el-checkbox
              :model-value="pickedIds.includes(m.id)"
              @change="togglePick(m.id)"
            >
              <span class="mono">{{ m.owner }} / {{ m.file_key }}</span>
            </el-checkbox>
            <span class="mono text-2">{{ span(m.indices) }}（{{ m.block_count }} 块）</span>
          </div>
        </div>

        <div class="mt-2">
          <el-button
            type="primary"
            :disabled="!pickedIds.length"
            :loading="fileVerifyRunning"
            @click="verifyPicked"
          >验证选中的 {{ pickedIds.length }} 个（共 {{ pickedBlocks }} 块）</el-button>
        </div>
      </template>

      <div v-if="fileQueryResult" class="mt-3">
        <VerifyResult :result="fileQueryResult" />
        <div class="mono text-2 mt-2" style="font-size: 12px">
          一份证据覆盖 {{ fileQueryResult.indices?.length }} 块 · {{ fileQueryResult.proof?.size_bytes }} 字节
        </div>
        <BlockFingerprints
          v-if="detailed"
          :indices="fileQueryResult.indices || []"
          :values="fileQueryResult.values || []"
          :refs="fileQueryResult.refs || []"
        />
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
</style>
