<script setup>
/**
 * 文件与块。
 *
 * - 上传卡：选文件 + file_key + 切法三档（自动/按块大小/按块数）+ 存到哪几台。
 * - 上传前先问 /api/plan（两档口径一起问），把候选切法摆成一张表；ok:false 显著提示。
 * - 文件列表：owner / file_key / 块数 / 大小 / 版本 / 全局下标(span) / 两把锁；
 *   行级操作直接摆在「操作」列：**详情 / 入池（已在池子里就是刷新）/ 试解密**。
 *   —— 「详情」「入池」不信**点文件标识**去猜（太隐蔽）。
 * - 「试解密」不置灰：点下去才看到后端真的拒了你（演示亮点）。
 */
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { filesApi } from '../../../api/files'
import { devicesApi } from '../../../api/devices'
import { systemApi } from '../../../api/system'
import { evidenceApi } from '../../../api/evidence'
import { useAuthStore } from '../../../stores/auth'
import { usePoolStore } from '../../../stores/pool'
import { useCryptoStore } from '../../../stores/crypto'
import { openBlocks } from '../../../utils/crypto/index'
import { forgetAnchorsOf, getDeltaAnchorFor } from '../../../utils/anchor'
import { span, fmtBytes, hexPreview } from '../../../utils/format'
import { SPLIT_MODES } from '../../../utils/constants'
import PageHeader from '../../../components/common/PageHeader.vue'
import LockTag from '../../../components/security/LockTag.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'
import EmptyState from '../../../components/common/EmptyState.vue'
import Icon from '../../../components/icons/Icon.vue'

const auth = useAuthStore()
const pool = usePoolStore()
const router = useRouter()

const loading = ref(false)
const error = ref('')
const files = ref([])
const nodes = ref([])
const status = ref(null)

// 上传表单
const fileRef = ref()
const uploadForm = reactive({
  fileKey: '',
  splitMode: 'auto',
  segmentBytes: 1024,
  blockCount: 4,
  pickedNodes: [],
})

const planAdvice = ref(null)
const askingPlan = ref(false)
const uploading = ref(false)
const uploadElapsed = ref(0)
const uploadTimings = ref(null)
//: 行级「入池」的忙碌状态与它的分步耗时（与上传那条共用同一个时间线组件）。
const poolBusy = ref(false)
const poolTimings = ref(null)
let elapsedTimer = null

const selectedFile = ref(null)

const maxBytes = computed(() => status.value?.segment_bytes_max || 1048576)
const minBytes = computed(() => status.value?.segment_bytes_min || 64)
const defaultBytes = computed(() => status.value?.segment_bytes || 1024)

/**
 * 本次上传会按多大的块切 —— **必须能看见**。
 *
 * ★★ 它就是「点过一次问顾问之后，无论怎么传都只切一块」那个 bug 的另一半：
 *   顾问只出**建议**，采纳要显式点建议卡里的按钮（见 :func:`applyPlan`）；
 *   不点，「自动」档永远等于**后端部署默认值**，与问没问过顾问无关。
 */
const effectiveSplit = computed(() => {
  const size = selectedFile.value?.size || 0
  const seg =
    uploadForm.splitMode === 'by_size'
      ? uploadForm.segmentBytes
      : uploadForm.splitMode === 'by_count'
        ? Math.max(1, Math.ceil(size / Math.max(1, uploadForm.blockCount)))
        : defaultBytes.value
  return { seg: Math.max(1, Math.round(seg || 0)) }
})

/** 按当前切法大约几块（与后端 split_segments 同一条口径：向上取整）。 */
const effectiveBlocks = computed(() => {
  const size = selectedFile.value?.size || 0
  if (!size) return null
  return Math.ceil(size / effectiveSplit.value.seg)
})

/**
 * 取消已选文件（点列表里那个 ×）。
 *
 * ★ 必须**连文件标识一起清掉**（用户反馈）：选文件时会自动把文件名填进
 *   「文件标识」（见 :func:`onFileChange`）。只清文件、不清标识的话，
 *   框里会留着上一个文件名 —— 看上去像是“还是那份文件”，很容易传错。
 */
function onFileRemove() {
  selectedFile.value = null
  uploadForm.fileKey = ''
  planAdvice.value = null
}

function onFileChange(f) {
  selectedFile.value = f?.raw || null
  // ★ 自动填「文件标识」：拿文件名去掉扩展名当默认值。**只在为空时填** ——
  //   免得把用户已经改过的名字覆盖掉（换个文件又得重打一遍）。
  if (selectedFile.value && !uploadForm.fileKey.trim()) {
    uploadForm.fileKey = String(f?.name || '').replace(/\.[^.]+$/, '').trim()
  }
  // ★ 换文件就把上一份建议丢掉：那个候选表是**按上一个文件的大小**算的，
  //   贴到新文件上给出的块数与估算都不是这个文件的。
  planAdvice.value = null
}

async function askPlan() {
  if (!selectedFile.value) return
  askingPlan.value = true
  planAdvice.value = null
  try {
    const size = selectedFile.value.size
    // ★ 同时问**两档口径**（接口里的 prefer）：顾问自己的规则只有「块数最少」
    //   （对 ≤1 MiB 的文件必然建议 1 块）与「改块粒度最细」两条。
    //   只问一条、再把那一句当圣旨摆出来，就是之前那个「建议共 1 块」的观感来源 ——
    //   那不是错，是**单一口径被当成了结论**。两档一起给，取舍摆在用户面前。
    const [fewest, finer] = await Promise.all([
      devicesApi.plan(size, 'fewest_blocks'),
      devicesApi.plan(size, 'finer_updates'),
    ])
    planAdvice.value = { size, fewest: fewest.data, finer: finer.data, defaultBytes: defaultBytes.value }
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '顾问询问失败')
  } finally {
    askingPlan.value = false
  }
}

/**
 * 候选表：顾问给的 15 档金字塔 + 「部署默认」那一行（若不在金字塔里），
 * 并标出哪几行是「最快 / 最细 / 默认」三个常用口径。
 */
const planRows = computed(() => {
  const a = planAdvice.value
  if (!a) return []
  const rows = (a.fewest?.alternatives || []).map((r) => ({ ...r, tags: [] }))
  const d = a.defaultBytes
  if (d && !rows.some((r) => r.segment_bytes === d)) {
    rows.push({
      segment_bytes: d,
      blocks: Math.ceil(a.size / d),
      est_upload_ms: null,
      est_verify_ms: null,
      allowed: null,
      note: '',
      tags: [],
    })
  }
  rows.sort((x, y) => x.segment_bytes - y.segment_bytes)
  for (const r of rows) {
    if (r.segment_bytes === a.fewest?.segment_bytes) r.tags.push('最快')
    if (r.segment_bytes === a.finer?.segment_bytes) r.tags.push('最细')
    if (r.segment_bytes === d) r.tags.push('默认')
  }
  return rows
})

/**
 * 采纳某一档 —— **只有点了这里，建议才会变成上传参数**。
 *
 * 采纳方式刻意选「切到『按块大小』那一档 + 把这一档的值填进去」，而不是偷偷改
 * 「自动」档的行为：这样档位与数字都摆在表单上，用户看得见、也随时能改回去。
 */
function applyPlan(seg) {
  if (!seg) return
  uploadForm.segmentBytes = Math.min(maxBytes.value, Math.max(minBytes.value, seg))
  uploadForm.splitMode = 'by_size'
  ElMessage.success(`已按这一档切：每块 ${uploadForm.segmentBytes} 字节`)
}

/** 这份文件是不是已经在池子里了（判据与 pool.addCard 的去重完全一致）。 */
function inPool(row) {
  return pool.indexKeys.has((row.indices || []).join(','))
}

/**
 * 「入池」：把这份文件的块取一份证据，攒进证据池。
 *
 * ★ 池子按**下标集合**去重（``stores/pool.js`` 的 ``addCard``）：命中时不新增卡片，
 *   而是用刚取到的最新证据**刷新**原来那张（δ 变过时这正好让它重新生效）。
 *   这件事必须明说 —— 不说的话，用户会以为点了两次就是两份，或者以为第二次没生效。
 */
async function addToPool(row, { silent = false } = {}) {
  // ★ 返回 boolean：批处理（一键入池）靠它算**真实的**成功数。
  //   以前这里只是裸 `return`、异常也被吞掉，于是 `poolAll` 里
  //   `ok += 1` 恒执行 —— 成功计数恒等于总数，用户在二次确认框里点“取消”
  //   也算成功（审计 F1）。
  const indices = row.indices || []
  if (!indices.length) {
    if (!silent) ElMessage.warning('这份文件没有块下标')
    return false
  }
  const already = inPool(row)
  if (already && !silent) {
    try {
      await ElMessageBox.confirm(
        `这 ${indices.length} 块已经在池子里了。\n\n` +
          '继续只会用刚取到的证据刷新卡片，不会新增。',
        '这份已在池子里',
        { type: 'warning', confirmButtonText: '刷新那一张', cancelButtonText: '取消' },
      )
    } catch {
      return false
    }
  }
  poolBusy.value = true
  poolTimings.value = null
  try {
    const t0 = performance.now()
    const { data } = await evidenceApi.query(indices, false)
    const ms = Math.round(performance.now() - t0)
    pool.addCard({
      label: `${row.owner}/${row.file_key}`,
      src: `来自文件列表 · ${row.block_count} 块`,
      result: data,
    })
    poolTimings.value = data.timings || null
    // ★ 耗时提示：**单文件**的取证是“把各节点的凭证聚合起来”，与块数近乎
    //   线性、很快；而一旦把**多份文件**的块放进同一次验证，就要在合并
    //   位置集上重算一份证据（秒级）。第一次入池就把这件事说清楚，
    //   免得用户以为“验证卡住了”。
    //
    //   `silent` 是批处理（一键入池）用的：一份弹一条只会把界面刷满，
    //   进度与总结由调用方统一说。
    if (!silent) {
      ElMessage.success(
        already
          ? `已刷新池子里那张卡（δ 指纹 ${data.delta_fp}，用时 ${ms} ms）`
          : `已加入证据池：${(data.indices || indices).length} 块合成一份证据`
              + `（用时 ${ms} ms）。跨文件验证会在合并位置集上重算证据，`
              + `通常要 1~3 秒。`,
      )
    }
  } catch {
    // 403 / 409 的中文理由已由拦截器原样弹出（“只有所有者”那条同样适用）
    return false
  } finally {
    poolBusy.value = false
  }
  return true
}

function buildFormData() {
  const fd = new FormData()
  fd.append('file', selectedFile.value)
  fd.append('file_key', uploadForm.fileKey.trim())
  // 切法 → segment_bytes
  let seg = null
  if (uploadForm.splitMode === 'auto') {
    // ★★ 「自动」= **后端部署默认切法**：不传 segment_bytes，由后端用
    //   Settings.segment_bytes（1024 字节/块）。
    //
    //   这里曾经写的是 ``seg = plan.value?.ok ? plan.value.segment_bytes : null``
    //   —— 那正是「点过一次问顾问之后，无论怎么传都只切一块」的根源：
    //   顾问的口径是「块数最少」，块上限 1 MiB，所以 ≤1 MiB 的文件它必然
    //   建议 1 块；而 plan 不会自己失效，于是「自动」档被它永久劫持。
    //   要采纳建议就走 applyPlan（显式切到「按块大小」档）。
    seg = null
  } else if (uploadForm.splitMode === 'by_size') {
    seg = uploadForm.segmentBytes
  } else if (uploadForm.splitMode === 'by_count') {
    seg = Math.max(1, Math.ceil(selectedFile.value.size / uploadForm.blockCount))
  }
  if (seg != null) fd.append('segment_bytes', String(seg))
  if (uploadForm.pickedNodes.length) {
    fd.append('nodes', uploadForm.pickedNodes.join(','))
  }
  return fd
}

async function doUpload() {
  // ★ 不靠"按钮置灰"拦：置灰只说明"还不能点"，说不清**为什么**。
  //   这里把两种情况分开说，而且**一个请求都不发**（实测 0 次 POST）。
  if (!selectedFile.value) {
    ElMessage.warning('请先选择要上传的文件')
    return
  }
  if (!uploadForm.fileKey.trim()) {
    ElMessage.warning('未填写文件标识')
    return
  }
  uploading.value = true
  uploadTimings.value = null
  uploadElapsed.value = 0
  elapsedTimer = setInterval(() => (uploadElapsed.value += 100), 100)
  try {
    const { data } = await filesApi.upload(buildFormData())
    uploadTimings.value = data.timings
    ElMessage.success('上传完成')
    resetUpload()
    await load()
  } catch {
    // 错误已由拦截器弹出（含 503 写推失败、413 超限等）
  } finally {
    clearInterval(elapsedTimer)
    uploading.value = false
  }
}

function resetUpload() {
  selectedFile.value = null
  uploadForm.fileKey = ''
  planAdvice.value = null
  if (fileRef.value) fileRef.value.clearFiles()
}

async function tryDecrypt(file) {
  // ★★ 默认模型下**服务端不持有私钥**（`server_key=false`）——
  //    `/api/files/{id}/decrypt`（服务端解密）会回
  //    “服务端这次会话里没有你的私钥”，即**必然失败**。
  //
  //    旧注释写的是“不置灰：点下去才看到后端真的拒了你（演示亮点）”，
  //    那是**旧模型**（后端扣着私钥）的说法 —— 默认模型下它变成了
  //    “一个永远报错的按钮”，用户只会以为系统坏了。
  //
  //    所以这里改走**客户端**那条路：取密文 → 在浏览器里解封块密钥 → SM4 解密
  //    → 顺手做一次本地验证。这才是“试解密”在这套架构下应有的样子。
  const crypt = useCryptoStore()
  if (!crypt.unlocked) {
    ElMessage.warning('浏览器里还没有私钥 —— 请重新登录一次（登录时用口令在本地解封），再试解密')
    return
  }
  if (!file.can_decrypt) {
    ElMessage.warning(
      `这不是你的文件（所有者：${file.owner}）—— 拿不到块密钥，解不开。` +
        '完整性验证是公开的，点「详情」就能看到验证结果。',
    )
    return
  }
  const tip = ElMessage({ message: `正在浏览器里试解密 ${file.file_key} …`, duration: 0 })
  try {
    const { data: pack } = await filesApi.cipher(file.id, null)
    // ★★ 核对锚的**归属**：`file.id` 会被库复用，旧文件的锚会让新文件
    //    误报“验证不通过”（见 `getDeltaAnchorFor` 的说明）。
    const opened = openBlocks(pack, crypt.key, {
      trustedDelta: getDeltaAnchorFor(file.id, file.content_digest),
    })
    const len = opened.plain.length
    const head = new TextDecoder('utf-8', { fatal: false }).decode(opened.plain.slice(0, 48))
    const verdict =
      { 'no-anchor': '本地还没钉过（本次用的是服务端给的 δ）', match: '与本地钉住的一致', mismatch: '与本地钉住的不一致（已用本地 δ 定性）' }[
        opened.deltaVerdict
      ] ?? opened.deltaVerdict
    await ElMessageBox.alert(
      `文件：${file.owner} / ${file.file_key}\n` +
        `解密：${len} 字节（在你的浏览器里解出来的，服务端没参与）\n` +
        `本地验证：${opened.verify.ok ? '通过' : `未通过 —— ${opened.verify.message}`}\n` +
        `δ 钉扎：${verdict}`,
      '试解密结果（浏览器本地）',
      { confirmButtonText: '知道了' },
    )
    if (head) {
      // 单独弹一个“看得到内容”的提示：这是“真的解出来了”的最直观证据
      ElMessage({
        message: `开头 48 字节：${head.replace(/\s+/g, ' ').trim() || '（不可打印）'}`,
        duration: 8000,
        showClose: true,
      })
    }
  } catch (e) {
    ElMessage.error(e?.message || '本地解密失败')
  } finally {
    tip.close()
  }
}

/**
 * 删除整份文件（**只有所有者**）。
 *
 * ★ **不再连带删别的文件**。旧设计里各文件挤在一条连续下标上，"删中间一份"
 *   就得把它后面的全部一起删掉；新方案里每份文件各占自己的位置段
 *   （位置互不重叠，也**永不回收**），所以删一份文件就是删**它自己**。
 *   原来那套"列出连带名单 / 后面压着别人的文件就拒绝"的判断已经全部不成立 ——
 *   留着只会让用户以为要连带删掉别人的东西而不敢操作（或者反过来被误导）。
 *
 * ★ 同时也**不再有 409**：删自己的东西碰不到别人的块。
 *
 * ★ 确认框仍然必须有：删除不可撤销，密文与封装过的块密钥都会真的没了。
 *   这是**真的丢数据**，不能静默。
 */
/**
 * 位置段 → 可读区间：``[[0, 20], [48, 2]]`` → ``0-19、48-49``。
 *
 * ★ 为什么不写 ``0+20``：那是“偏移 + 块数”的记账写法，和尾部再拼一个
 *   “20 块”放一起会变成 ``0+2020 块``，谁也读不出来。区间写法一眼能看懂，
 *   而“共几块”已经有独立的「块数」列，不必在这里重复。
 */
function segText(segments) {
  const out = (segments || []).map(([o, c]) => (c <= 1 ? `${o}` : `${o}-${o + c - 1}`))
  return out.length ? out.join('、') : '—'
}

async function removeFile(row, { silent = false } = {}) {
  const parts = [
    `将删除 ${row.owner}/${row.file_key}（${row.block_count} 块）。`,
    '删除不可撤销：这份文件的密文、封装过的块密钥与账目都会从库里、从节点上删掉。',
    '别的文件不受影响 —— 每份文件各占自己的位置段，删它不会动到任何别人。',
  ]
  if (!silent) {
    try {
      await ElMessageBox.confirm(parts.join('\n'), '确认删除', {
        type: 'warning',
        confirmButtonText: '删除',
        cancelButtonText: '取消',
      })
    } catch {
      return false
    }
  }
  try {
    const { data } = await filesApi.remove(row.id)
    // ★★ 删掉之后**本地锚必须一起清**：锚是按 `fileId` 存的，而库会复用
    //    id（SQLite 的 `INTEGER PRIMARY KEY` 没有 AUTOINCREMENT）——
    //    不清的话，下一次上传的文件拿到同一个 id，解密时会**拿这份旧文件的
    //    基准去验新文件**，报“本地验证未通过”（实测踩过，改名也躲不开）。
    //
    //    ⚠️ `data.deleted_files` 可能**多于一份**（删末尾会连带删掉它后面的
    //    文件），但接口只给名字不给 id，所以这里只能清**当前这一份**的锚。
    //    被连带删掉的那些，靠锚的 `digest` 归属核对兜住（见 anchor.js）。
    forgetAnchorsOf(row.id)
    const n = (data.deleted_files || []).length
    if (!silent) {
      ElMessage.success(
        `已删除 ${data.dropped_blocks} 块` + (n > 1 ? `（共 ${n} 份文件）` : ''),
      )
    }
  } catch {
    // 错误已由拦截器弹出
    return false
  }
  // 单份删除后要刷新列表；批量走 `removeAll`，它在最后统一刷一次
  // （每份都刷会把列表闪 n 次）。
  if (!silent) await load()
  return true
}

/** 我能解密的那些文件（= 我自己的）——“一键”两个按钮的作用对象。 */
const mineFiles = computed(() => (files.value || []).filter((r) => r.can_decrypt))

const poolAllBusy = ref(false)
const removeAllBusy = ref(false)

/**
 * 「一键入池」：把**自己全部**的文件各取一份证据收进证据池。
 *
 * ★ 逐个串行，**不并发**：每次入池都要向“持有那几块的节点”取密文，
 *   并发打上去只会把节点压住，还可能撞上协调者的写锁。一份一份走，
 *   进度用一条不自动关的提示显示（第 i / n 份）。
 *
 * ★ 单份失败**不打断**整批 —— 最后统一报“成功几份”。否则一份坏文件
 *   就会把后面全卡住，而且用户不知道卡在哪。
 */
async function poolAll() {
  const list = mineFiles.value
  if (!list.length) return
  poolAllBusy.value = true
  let ok = 0
  const tip = ElMessage({ message: `正在入池 0 / ${list.length} …`, duration: 0 })
  try {
    for (let i = 0; i < list.length; i++) {
      tip.message = `正在入池 ${i + 1} / ${list.length}：${list[i].file_key} …`
      try {
        // ★ 按**返回值**计数，不是“调用过就算成功”（审计 F1）。
        if (await addToPool(list[i], { silent: true })) ok += 1
      } catch {
        /* 单份失败不打断 */
      }
    }
  } finally {
    tip.close()
    poolAllBusy.value = false
  }
  if (ok === list.length) {
    ElMessage.success(`一键入池：${ok} / ${list.length} 份已收进证据池`)
  } else {
    ElMessage.warning(
      `一键入池：成功 ${ok} / ${list.length} 份，` +
        `${list.length - ok} 份没进去（失败原因上面已经逐条说过）`,
    )
  }
}

/**
 * 「一键删除」：把**自己全部**的文件删掉。
 *
 * ★★ 这是本页破坏性最强的按钮，所以两道门：
 *   ① 先把要删的逐条列出来（名字 + 块数），不是一句“确定吗”；
 *   ② 必须**手工输入** `DELETE` 才放行 —— 它旁边就是“一键入池”，
 *      一个误点就没了的东西不能只看一眼回车。
 */
async function removeAll() {
  const list = mineFiles.value
  if (!list.length) return
  const lines = list.map((r) => `  · ${r.owner}/${r.file_key}（${r.block_count} 块）`)
  try {
    await ElMessageBox.prompt(
      [
        `将删除以下 ${list.length} 份文件的全部内容：`,
        ...lines,
        '',
        '删除不可撤销：密文、封装过的块密钥与账目都会从库里、从节点上删掉。',
        '请输入大写的 DELETE 确认：',
      ].join('\n'),
      '确认删除全部',
      {
        confirmButtonText: '全部删除',
        cancelButtonText: '取消',
        type: 'warning',
        inputPattern: /^DELETE$/,
        inputErrorMessage: '请输入大写的 DELETE 才能继续',
      },
    )
  } catch {
    return
  }
  removeAllBusy.value = true
  let ok = 0
  const tip = ElMessage({ message: `正在删除 0 / ${list.length} …`, duration: 0 })
  try {
    for (let i = 0; i < list.length; i++) {
      tip.message = `正在删除 ${i + 1} / ${list.length}：${list[i].file_key} …`
      try {
        // ★ 按**返回值**计数（审计 F1）；用户中途取消不计成功。
        if (await removeFile(list[i], { silent: true })) ok += 1
      } catch {
        /* 单份失败不打断 */
      }
    }
  } finally {
    tip.close()
    removeAllBusy.value = false
  }
  await load()
  if (ok === list.length) {
    ElMessage.success(`一键删除：${ok} / ${list.length} 份已删除`)
  } else {
    ElMessage.warning(
      `一键删除：成功 ${ok} / ${list.length} 份，` +
        `${list.length - ok} 份没删掉（失败原因上面已经逐条说过）`,
    )
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [f, n, s] = await Promise.all([filesApi.list(), devicesApi.nodes(), systemApi.status()])
    files.value = f.data
    nodes.value = n.data
    status.value = s.data
    // 默认全选节点
    if (!uploadForm.pickedNodes.length) {
      uploadForm.pickedNodes = n.data.filter((x) => !x.unreachable).map((x) => x.node_id)
    }
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <PageHeader title="文件与块" subtitle="所有人可验证；持有者可解密" />

    <div v-if="error" class="panel"><p class="text-danger">{{ error }}</p></div>
    <template v-else>
      <div class="panel mb-3">
        <h4 class="sec-title">上传文件</h4>
        <div class="upload-row">
          <el-upload
            ref="fileRef"
            :auto-upload="false"
            :show-file-list="true"
            :limit="1"
            :on-change="onFileChange"
            :on-remove="onFileRemove"
            drag
            class="upload-drag"
          >
            <Icon name="upload" :size="28" />
            <div class="el-upload__text">拖文件到这里，或点击选择</div>
          </el-upload>

          <div class="upload-form">
            <el-input v-model="uploadForm.fileKey" placeholder="文件标识 file_key（如 病历A）" />
            <el-radio-group v-model="uploadForm.splitMode">
              <el-radio-button v-for="m in SPLIT_MODES" :key="m.value" :value="m.value">{{ m.label }}</el-radio-button>
            </el-radio-group>
            <div v-if="uploadForm.splitMode === 'by_size'" class="mono">
              <span class="text-2">块大小（字节）：</span>
              <el-input-number v-model="uploadForm.segmentBytes" :min="status?.segment_bytes_min || 64" :max="status?.segment_bytes_max || 1048576" :step="64" />
            </div>
            <div v-else-if="uploadForm.splitMode === 'by_count'" class="mono">
              <span class="text-2">切成几块：</span>
              <el-input-number v-model="uploadForm.blockCount" :min="1" :max="8192" />
            </div>

            <div v-if="selectedFile" class="effective mono">
              本次切法：每块 {{ effectiveSplit.seg }} 字节<template v-if="effectiveBlocks"> · 约 {{ effectiveBlocks }} 块</template>
              <span v-if="uploadForm.splitMode === 'auto'" class="text-3">（部署默认）</span>
            </div>

            <div class="nodes-pick">
              <span class="text-2">存到哪几台：</span>
              <el-checkbox-group v-model="uploadForm.pickedNodes">
                <el-checkbox v-for="n in nodes" :key="n.node_id" :value="n.node_id" :disabled="n.unreachable">
                  <span class="mono">{{ n.node_id }}</span>
                </el-checkbox>
              </el-checkbox-group>
            </div>

            <div class="actions">
              <el-button :disabled="!selectedFile" :loading="askingPlan" @click="askPlan">问顾问</el-button>
              <el-button type="primary" :loading="uploading" @click="doUpload">上传</el-button>
              <span v-if="uploading" class="mono elapsed">已用 {{ (uploadElapsed / 1000).toFixed(1) }} s</span>
            </div>
          </div>
        </div>

        <div v-if="planAdvice" class="plan-box">
          <el-alert
            v-if="!planAdvice.fewest.ok"
            type="error"
            :closable="false"
            class="mb-2"
            title="放不下"
            :description="planAdvice.fewest.reason"
          />
          <div class="plan-head">
            顾问给了 {{ planRows.length }} 种切法。<b>点某一行的「用这一档」才会采用</b>，
            不点就按上面的「本次切法」走。带「最快 / 最细 / 默认」标签的是三种常见选择。
          </div>
          <div class="plan-table">
            <div class="pt-row pt-head mono">
              <span>每块</span><span>块数</span><span>估算（上传 / 验证）</span><span>说明</span><span></span>
            </div>
            <div
              v-for="r in planRows"
              :key="r.segment_bytes"
              class="pt-row"
              :class="{ 'is-bad': r.allowed === false }"
            >
              <span class="mono">{{ r.segment_bytes }} B</span>
              <span class="mono">{{ r.blocks }} 块</span>
              <span class="text-3 mono">{{ r.est_upload_ms == null ? '—' : `${r.est_upload_ms} / ${r.est_verify_ms} ms` }}</span>
              <span>
                <el-tag
                  v-for="t in r.tags"
                  :key="t"
                  size="small"
                  effect="plain"
                  style="margin-right: 4px"
                >{{ t }}</el-tag>
                <span v-if="r.note" class="text-3" style="font-size: 11px">{{ r.note }}</span>
              </span>
              <el-button
                link
                type="primary"
                size="small"
                :disabled="r.allowed === false"
                @click="applyPlan(r.segment_bytes)"
              >用这一档</el-button>
            </div>
          </div>
          <el-collapse class="why">
            <el-collapse-item title="为什么这么建议" name="why">
              <p class="text-2" style="font-size: 12px">【块数最少】{{ planAdvice.fewest.why }}</p>
              <p class="text-2" style="font-size: 12px">【粒度最细】{{ planAdvice.finer.why }}</p>
              <p class="text-3" style="font-size: 11px">估算来源：{{ planAdvice.fewest.measured_source }}</p>
            </el-collapse-item>
          </el-collapse>
        </div>

        <StageTimeline v-if="uploadTimings" :timings="uploadTimings" class="mt-3" />
      </div>

      <div class="panel">
          <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px">
            <h4 class="sec-title" style="margin: 0">文件列表</h4>
            <div style="display: flex; gap: 8px">
              <el-button
                size="small"
                :loading="poolAllBusy"
                :disabled="!mineFiles.length"
                @click="poolAll"
              >
                一键入池（{{ mineFiles.length }} 份）
              </el-button>
              <el-button
                size="small"
                type="danger"
                plain
                :loading="removeAllBusy"
                :disabled="!mineFiles.length"
                @click="removeAll"
              >
                一键删除（{{ mineFiles.length }} 份）
              </el-button>
            </div>
          </div>
        <el-table :data="files" v-loading="loading" border>
          <el-table-column prop="owner" label="所有者" width="110">
            <template #default="{ row }"><span class="mono">{{ row.owner }}</span></template>
          </el-table-column>
          <el-table-column label="文件标识" min-width="170">
            <template #default="{ row }">
              <el-link type="primary" @click="router.push(`/files/${row.id}`)">{{ row.file_key }}</el-link>
              <el-tag
                v-if="inPool(row)"
                size="small"
                type="success"
                effect="plain"
                style="margin-left: 6px"
              >已入池</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="block_count" label="块数" width="70" align="center" />
          <el-table-column label="大小" width="90">
            <template #default="{ row }"><span class="mono">{{ fmtBytes(row.total_bytes) }}</span></template>
          </el-table-column>
          <el-table-column label="版本" width="70" align="center">
            <template #default="{ row }"><span class="mono">{{ row.version }}</span></template>
          </el-table-column>
          <el-table-column label="位置段" min-width="150">
            <template #default="{ row }">
              <!-- ★ 新方案（一文件一向量）：界面按「文件 / 第几块」说话，
                   这里给的是**内部坐标**（这份文件在全局素数表里占的段）。
                   段可能有好几截 —— 追加时原段末尾被后来的文件占住了，
                   就只能另起一段，所以“块号”与“位置号”本来就不该画等号。
                   ★ 渲染成**区间**（0-19）而不是 `偏移+块数`：后者写作 `0+20`，
                   再拼上尾部那个“20 块”就变成 `0+2020 块` —— 读不出来。 -->
              <span class="mono" :title="`内部坐标：${row.segments?.length || 0} 段`">
                {{ segText(row.segments) }}
              </span>
            </template>
          </el-table-column>
          <el-table-column label="验证" width="80" align="center">
            <template #default>
              <span class="text-accent" title="谁都能验证">可验证</span>
            </template>
          </el-table-column>
          <el-table-column label="解密" width="90" align="center">
            <template #default="{ row }">
              <LockTag :can-decrypt="row.can_decrypt" />
            </template>
          </el-table-column>
          <el-table-column label="操作" width="250" align="center">
            <template #default="{ row }">
              <el-button link type="primary" @click="router.push(`/files/${row.id}`)">详情</el-button>
              <el-button link type="primary" :loading="poolBusy" @click="addToPool(row)">
                {{ inPool(row) ? '刷新' : '入池' }}
              </el-button>
              <el-button link type="primary" @click="tryDecrypt(row)">试解密</el-button>
              <el-button
                v-if="row.is_mine"
                link
                type="danger"
                @click="removeFile(row)"
              >删除</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <EmptyState title="还没有文件" description="上传一份试试，切法随你选" />
          </template>
        </el-table>
        <StageTimeline v-if="poolTimings" :timings="poolTimings" class="mt-3" />
      </div>
    </template>
  </div>
</template>

<style scoped>
/* ★ 上传列表里那个 × 默认只有 14px，很难点中（用户反馈：做大一点）。
   给它一个 24px 的**可点面积**，而不是只有字形本身。 */
.upload-drag :deep(.el-upload-list__item .el-icon--close) {
  font-size: 20px;
  width: 24px;
  height: 24px;
  line-height: 24px;
  text-align: center;
  border-radius: 50%;
}
.upload-drag :deep(.el-upload-list__item .el-icon--close:hover) {
  background: var(--bg-raised, rgba(0, 0, 0, 0.08));
}
.sec-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 14px;
}
.upload-row {
  display: flex;
  gap: 20px;
  flex-wrap: wrap;
}
.upload-drag {
  width: 260px;
}
.upload-form {
  flex: 1;
  min-width: 300px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.nodes-pick {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
.elapsed {
  font-size: 12px;
  color: var(--accent);
}
.plan-box {
  margin-top: 14px;
  padding: 12px;
  background: var(--bg-raised);
  border-radius: var(--radius-sm);
  border: 1px solid var(--line);
  font-size: 13px;
}
.alt {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.alt-row {
  display: flex;
  gap: 16px;
  font-size: 12px;
}
.effective {
  font-size: 12px;
  color: var(--text-2);
}
.plan-actions {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 10px;
}
.plan-head {
  font-size: 12px;
  color: var(--text-2);
  margin-bottom: 10px;
}
.plan-table {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.pt-row {
  display: grid;
  grid-template-columns: 92px 70px minmax(120px, 1fr) minmax(140px, 1.2fr) 84px;
  gap: 10px;
  align-items: center;
  font-size: 12px;
  padding: 3px 6px;
  border-radius: 4px;
}
.pt-row:not(.pt-head):hover {
  background: color-mix(in srgb, var(--accent) 6%, transparent);
}
.pt-head {
  color: var(--text-3);
  font-size: 11px;
}
.pt-row.is-bad {
  opacity: 0.55;
}
.why {
  margin-top: 10px;
}
</style>
