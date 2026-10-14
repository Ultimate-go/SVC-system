<script setup>
/**
 * 文件详情 / 变更文件（★ 2026-10-12 拆页面：同一个组件两种形态，靠路由 meta 区分）。
 *
 *   · files-detail        → 「文件详情」：只读 —— 摘要与参数 / 封装过程 /
 *                            块分布矩阵 / 块明细；
 *   · files-update-detail → 「变更文件」：在上面的基础上多出更新 / 置零 /
 *                            追加 / 截断 / 回滚演示 / 解密验证。
 *
 * ★ 为什么复用同一个组件而不是拷一份：这些能力读的是**同一份状态**
 *   （file / layout / δ 本地锚），拆成两个文件就会各拉一次详情、各维护一份锚 ——
 *   迟早分叉。用一个形态开关，改动只有一份代码。
 *
 * - 文件元信息 + 版本 + delta_n / delta_fp。
 * - 块分布矩阵（layout[].replicas 标主/副本）。
 * - **块明细表**：每块的公开分量（指纹）—— 跟着「简略/详细」开关
 *   决定要不要铺出来；详细模式会把每块的分量一起要回来（`?elements=1`）。
 * - 改块 / 追加 / 截断（三个写操作都显示 timings）。
 * - 解密预览（**浏览器本地解密 + 本地验证**；hex → hexToText，用 hasBadBytes 区分残缺）。
 */
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { filesApi } from '../../../api/files'
import { devicesApi } from '../../../api/devices'
import { useAuthStore } from '../../../stores/auth'
import { useThemeStore } from '../../../stores/theme'
import {
  span,
  fmtBytes,
  fmtTime,
  hexFp,
  humanIdx,
  humanList,
  decodeBlockHex,
  hasBadBytes,
  estimateCipherPackBytes,
  CIPHER_PACK_WARN_BYTES,
} from '../../../utils/format'
import { BLOCK_PREVIEW_LIMIT } from '../../../utils/constants.js'
import { useCryptoStore } from '../../../stores/crypto'
import { openBlocks } from '../../../utils/crypto/index.js'
import { bytesToHex } from '../../../utils/crypto/bytes.js'
import { getAnchor, setAnchor, sha256Hex, fmtWhen, getDeltaAnchorFor, setDeltaAnchor, dropDeltaAnchor, getCrsAnchor, setCrsAnchor, crsFingerprint, matchHistory } from '../../../utils/anchor'
import PageHeader from '../../../components/common/PageHeader.vue'
import BlockPreviewBar from '../../../components/common/BlockPreviewBar.vue'
import DetailToggle from '../../../components/common/DetailToggle.vue'
import HashText from '../../../components/common/HashText.vue'
import BlockMatrix from '../../../components/chart/BlockMatrix.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'
import LockTag from '../../../components/security/LockTag.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const theme = useThemeStore()

const id = computed(() => route.params.id)

/** 更新形态（改块 / 追加 / 截断 / 回滚演示 / 解密验证）开关，见文件头的说明。 */
const showOps = computed(() => route.meta?.ops === true)
/** 返回按钮的去处：从哪进来就回哪去。 */
const backTo = computed(() => (showOps.value ? '/files/update' : '/files'))
const pageTitle = computed(() =>
  file.value
    ? `${file.value.owner} / ${file.value.file_key}`
    : showOps.value
      ? '更新文件'
      : '文件详情',
)

const loading = ref(false)
const error = ref('')
const file = ref(null)
const nodes = ref([])

/**
 * 块明细表铺不铺开。
 *
 * ★ 超过 `BLOCK_PREVIEW_LIMIT` 块时先只列前那么多 —— 一份几百块的文件全铺出来，
 *   页面会长到没法看（用户反馈）；点「展示详情」再铺开。
 *   换一份文件就收回：不然“我上次展开过”会跟着走到下一份文件上。
 */
const blockPreviewOpen = ref(false)
watch(id, () => {
  blockPreviewOpen.value = false
})

/** 块明细表实际渲染的行（收起来时只给前 `BLOCK_PREVIEW_LIMIT` 块）。 */
const blockRows = computed(() => {
  const rows = file.value?.layout || []
  if (blockPreviewOpen.value || rows.length <= BLOCK_PREVIEW_LIMIT) return rows
  return rows.slice(0, BLOCK_PREVIEW_LIMIT)
})

/**
 * 「简略 / 详细」—— 详细模式要多要一份数据：每块的分量（`?elements=1`）。
 *
 * ★ 所以它一变就要**重新拉一次详情**，而不是只切 `v-if`：简略模式那次
 *   响应里**根本没有** element 字段（后端按参数决定给不给），
 *   光切 v-if 会看到一整列空白。
 */
const detailed = computed(() => theme.detailMode === 'detail')
watch(detailed, () => load())

// 写操作表单
const activeOp = ref('modify')
const modifyForm = reactive({ blockIdx: 0, text: '' })
const appendText = ref('')
const dropBlocks = ref(1)

/* ---------------------------------------------------------------------------
   块号的「输入框口径」（★ 2026-10-12，全项目统一）
   ---------------------------------------------------------------------------
   内部（API / δ / 锚）一律 **0 起**；界面上给人看的、给人填的一律 **1 起**。
   所以输入框要一个代理：读到界面上是 `idx + 1`，写回内部是 `值 - 1`。
   换算只在这一层做，下面所有业务代码继续用 0 起的 modifyForm.blockIdx。
--------------------------------------------------------------------------- */
const blockNoInput = computed({
  get: () => humanIdx(modifyForm.blockIdx),
  set: (v) => {
    modifyForm.blockIdx = Math.max(0, Number(v) - 1)
  },
})
const replayNoInput = computed({
  get: () => humanIdx(replayIdx.value),
  set: (v) => {
    replayIdx.value = Math.max(0, Number(v) - 1)
  },
})

const writeRunning = ref(false)
const writeTimings = ref(null)

// 解密
const decrypting = ref(false)
/**
 * 浏览器**本地开块**的结果（不再是"后端返回的明文"）。
 *
 * ★ 比旧结构多了一条 ``verify`` —— 因为现在**验证也在浏览器里做**：
 *   分量由前端从密文自己算，证据对着本地 δ 跑 add_back 链。
 *   于是"这次拿到的东西对不对"有了独立判据，不再依赖后端那句 `ok`。
 */
const decryptResult = ref(null)

const isMine = computed(() => file.value?.is_mine)

/** 浏览器侧的私钥状态（私钥只活在内存，见 stores/crypto.js）。 */
const crypt = useCryptoStore()

const decodePreview = computed(() => {
  if (!decryptResult.value?.hex) return null
  return decodeBlockHex(decryptResult.value.hex)
})

  // ---- 改块前“看旧内容”：明文 + **本地锚定校验** ----
  //
  // ★ 为什么不是“调个解密接口显示出来”就完了：这一版的解密在服务端做
  //   （前端没有 SM3/SM4，也没有私钥），所以“明文有没有被换”只能靠客户端
  //   自己的记录来判 —— 依据是本浏览器写这块时留下的明文 SHA-256。
  //   详见 utils/anchor.js 的长注释（证明什么、不证明什么）。
  const oldLoading = ref(false)
  const oldInfo = ref(null) // { blockIdx, bytes, sha256, hex, binary, anchor, verdict }

/** δ 承诺弹框：展示本文件承诺 δ = (U, C) 的完整值，以及对应的 12 位指纹。 */
const deltaDialogOpen = ref(false)

/**
 * 复制 δ 的某一项到剪贴板。
 *
 * :param which: ``'U'`` | ``'C'`` | ``'fp'`` —— 分别是两个承诺分量与短指纹。
 *    三项都是字符串（十进制 / 十六进制），直接写入剪贴板；
 *    失败只可能是浏览器未授予剪贴板权限。
 */
async function copyDelta(which) {
  const text =
    which === 'U' ? file.value?.delta_U : which === 'C' ? file.value?.delta_C : file.value?.file_delta_fp
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success(which === 'fp' ? 'δ 指纹已复制' : `承诺分量 ${which} 已复制`)
  } catch {
    ElMessage.error('复制失败：浏览器未授予剪贴板权限')
  }
}

  function hexToBytes(hex) {
    const out = new Uint8Array(Math.floor((hex || '').length / 2))
    for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16)
    return out
  }

  /** 记一次锚：内容是我自己写进去的（改块 / 清零），不需要信任任何服务器。 */
  async function anchorBytes(blockIdx, bytes, data, source) {
    await setAnchor(id.value, blockIdx, {
      sha256: await sha256Hex(bytes),
      version: data?.file?.version ?? data?.version ?? null,
      deltaFp: data?.file?.file_delta_fp ?? null,
      bytes: bytes.length,
      source,
    })
  }

  /**
   * 取回这一块当前的明文，并**先给结论、再给内容**。
   *
   * ★ 与上一版的区别是"结论从哪来"。上一版明文由服务端给出，只能靠本地锚
   *   判"有没有被换"；现在走 `/cipher` + 浏览器开块，多了一条**独立判据**：
   *   分量自己算、证据对着本地 δ 验 —— 服务端连"伪造一份能过验证的应答"
   *   都做不到（除非它同时改掉浏览器本地的 δ）。
   */
  async function fetchOldContent() {
    const idx = modifyForm.blockIdx
    oldLoading.value = true
    oldInfo.value = null
    const key = crypt.key
    if (!key) {
      ElMessage.warning('本机尚未持有私钥 —— 请重新登录一次（登录时以口令在本地解封）')
      oldLoading.value = false
      return
    }
    try {
      const { data: pack } = await filesApi.cipher(id.value, [idx])
      // ★ 用**本地钉住的 δ** 验（审计 S4）—— 没钉过才退回远端的那一份。
      //   ★★ 且必须核对锚的**归属**：`fileId` 会被库复用，旧文件的锚会让
      //      新文件误报“验证不通过”（见 `getDeltaAnchorFor` 的说明）。
      const opened = openBlocks(pack, key, {
        trustedDelta: getDeltaAnchorFor(id.value, file.value?.content_digest),
      })
      const block = opened.blocks[0]
      const bytes = block.plain
      const sha = await sha256Hex(bytes)
      const anchor = getAnchor(id.value, idx)
      // ★★ 旧数据检测：内容与当前锚不符时，再问一句“它命不命中我以前的那几版”。
      //   命中的话性质就明确了 —— 服务端交回来的是**改块前**的那份内容
      //   （回滚 / 缓存了旧版本）。不命中的话才是含糊的“内容不对劲”。
      const hist = matchHistory(anchor, sha)
      let text = ''
      let binary = false
      try {
        text = new TextDecoder('utf-8', { fatal: true }).decode(bytes)
      } catch {
        binary = true
        text = new TextDecoder('utf-8').decode(bytes)
      }
      oldInfo.value = {
        blockIdx: idx,
        bytes: bytes.length,
        sha256: sha,
        hex: bytesToHex(bytes),
        binary,
        anchor,
        // ★ 先看**密码学结论**（浏览器本地验证），再看锚。
        //   验证不过 ⇒ 内容不可信，锚对不对都不重要。
        verifyOk: opened.verify.ok,
        verifyMsg: opened.verify.message,
        /** δ 钉扎的结论：本地与远端的那一份是不是同一个版本。 */
        deltaVerdict: opened.deltaVerdict,
        elementAgrees: block.elementAgrees,
        ms: opened.ms,
        // ★★ `hist.isOld` 必须排在最前面 —— 它是最**确定**的结论。
        //    旧数据命中历史版本时，`verify.ok` 必然也是 false（旧密文算不出
        //    当前的 δ），所以把验证放前面会把“服务器在拿旧数据搪塞我”
        //    降级成含糊的“这一块不可信”，甚至被误解成“数据被改坏了”。
        //    两者性质不同：前者是服务器撒谎，后者是数据损坏。
        verdict: hist.isOld
          ? 'stale'
          : !opened.verify.ok
            ? 'verify-failed'
            : !anchor
              ? 'no-anchor'
              : anchor.sha256 === sha
                ? 'match'
                : 'mismatch',
        /** ★ 命中的旧版本记录（只有 `stale` 才有值）。 */
        staleFrom: hist.record,
      }
      // ★★ 把“这是旧数据”**明说出口**（用户反馈的第 5 条）：
      //   含糊的“内容与记录不一致”会让人以为是自己记错了；而命中历史版本是一个
      //   **确定的结论** —— 服务端把改块**前**的那份内容交回来了。
      if (hist.isOld) {
        ElMessage.warning({
          message:
            `服务端返回的内容命中了你在${fmtWhen(hist.record.ts)}更新的那一版` +
            `（${hist.record.source || '本地记录'}，第 ${hist.record.version ?? '—'} 版）：` +
            '该内容属于更新前的旧数据（版本回滚或缓存了旧版本），不是当前版本。' +
            '注意：本地仅保存其摘要（用于识别），因此只能给出上述结论，无法给出该版本的内容本身。',
          duration: 10000,
          showClose: true,
        })
      }
      // 旧内容直接填进“新内容”框（就地改最顺手）；二进制内容不填，免得写坏。
      if (!binary) modifyForm.text = text
    } catch (e) {
      ElMessage.error(e?.message || e?.response?.data?.detail || '取回失败')
    } finally {
      oldLoading.value = false
    }
  }

function toB64(str) {
  // 浏览器端用 TextEncoder 转 base64（UTF-8 安全）。
  const bytes = new TextEncoder().encode(str)
  let bin = ''
  bytes.forEach((b) => (bin += String.fromCharCode(b)))
  return btoa(bin)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [{ data: f }, { data: n }] = await Promise.all([
      filesApi.detail(id.value, detailed.value),
      devicesApi.nodes(),
    ])
    file.value = f
    nodes.value = n
    if (f.layout?.length) modifyForm.blockIdx = f.layout[0].block_idx
    // 回退面板要用它 —— 只在「变更文件」形态下拉（详情页不显示这块）。
    await loadHistory()
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

/* ---------------------------------------------------------------------------
   回退到上一版（★ 2026-10-12 条目 6）
   ---------------------------------------------------------------------------
   改块/清零**之前**，后端会自动把那一块当时的整套本体留一份（每块只留一份 =
   当前 + 上一版）。这里只做两件事：把“哪几块能回退”列出来，以及按一下回退。
   ★ 界面上不重复后端的话术：没有留存时就说“还没留过上一版”，不假装能回退。
--------------------------------------------------------------------------- */
const history = ref([])
const historyLoading = ref(false)
const restoring = ref(false)
const restoreResult = ref(null)

async function loadHistory() {
  if (!showOps.value) {
    history.value = []
    return
  }
  historyLoading.value = true
  try {
    const { data } = await filesApi.history(id.value)
    history.value = data?.blocks || []
  } catch {
    // 拿不到历史不影响别的操作 —— 不当成整页错误
    history.value = []
  } finally {
    historyLoading.value = false
  }
}

async function doRestore() {
  const blocks = history.value.map((h) => humanIdx(h.block_idx))
  try {
    await ElMessageBox.confirm(
      `将第 ${blocks.join('、')} 个分块替换为上一版本的本体，并重新计算聚合承诺。\n\n` +
        '回退将产生新的版本号（而非将版本号改回），并记入一条审计。\n' +
        '当前版本会被留存为新的「上一版本」，因此回退本身也可以再次回退。',
      '回退至上一版本',
      { type: 'warning', confirmButtonText: '回退', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  restoring.value = true
  restoreResult.value = null
  try {
    const { data } = await filesApi.restore(id.value, null)
    restoreResult.value = data
    ElMessage.success(`已回退 ${data.count} 个分块；当前为第 ${data.version} 版（聚合承诺已重算）`)
    await load()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '回退失败')
  } finally {
    restoring.value = false
  }
}

async function doModify() {
  const idx = modifyForm.blockIdx
  const text = modifyForm.text
  const body = { op: 'modify', block_idx: idx, data_b64: toB64(text) }
  writeRunning.value = true
  writeTimings.value = null
  try {
    const { data } = await filesApi.patch(id.value, body)
    writeTimings.value = data.timings
    // ★ 内容是我自己刚写进去的 —— 顺手把本地锚更新掉，下次看这块就有判据了。
    await anchorBytes(idx, new TextEncoder().encode(text), data, '更新')
    // ★ δ 变了（是**你自己**改的）—— 把本地钉的那一份丢掉，
    //   让下一次解密变成"重新见证"，而不是默默把新 δ 收下。
    dropDeltaAnchor(id.value)
    oldInfo.value = null
    ElMessage.success(`已更新第 ${humanIdx(idx)} 个分块；当前为第 ${data.version} 版（本地锚已更新）`)
    await load()
  } catch {
  } finally {
    writeRunning.value = false
  }
}

/** 那一条块账目（拿它的明文长度给确认框看）—— 简略模式下也有。 */
const zeroTarget = computed(() =>
  (file.value?.layout || []).find((b) => b.block_idx === modifyForm.blockIdx),
)

/**
 * 清零：把这一块换成**等长的全 0 字节**。
 *
 * ★ 它不是删除，是**改块**（服务端的 ``op = zero`` 走的就是 ``mod``）：
 *   块仍然在（下标不变、仍占存储、总块数不变），完整性照样验证通过，
 *   只是内容变了、版本号 +1。所以这里的文案必须说清“不是删掉它”，
 *   否则用户会以为清完那一块就不在了。
 */
async function doZero() {
  const idx = modifyForm.blockIdx
  const len = zeroTarget.value?.plain_len
  try {
    await ElMessageBox.confirm(
      `第 ${humanIdx(idx)} 个分块的内容将被替换为等长的全 0 字节${len ? `（${len} 字节）` : ''}。\n\n` +
        '分块序号、位置与总块数均不变，完整性校验仍可通过。\n' +
        '块密钥将重新封装，长度保持不变。\n\n原内容不可恢复，版本号递增 1。',
      '置零该分块',
      { type: 'warning', confirmButtonText: '置零', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  writeRunning.value = true
  writeTimings.value = null
  try {
    const { data } = await filesApi.zero(id.value, idx)
    writeTimings.value = data.timings
    // ★ 清零后的内容是**等长的全 0 字节**（长度就是原 plain_len）—— 客户端知道它，
    //   所以同样锚得下来，下次看这块一样能判服务器有没有换。
    await anchorBytes(idx, new Uint8Array(data.zeroed?.bytes ?? len ?? 0), data, '置零')
    dropDeltaAnchor(id.value)
    oldInfo.value = null
    ElMessage.success(
      `已置零第 ${humanIdx(idx)} 个分块（${data.zeroed?.bytes ?? '?'} 字节全 0，长度不变）；当前为第 ${data.version} 版`,
    )
    await load()
  } catch {
  } finally {
    writeRunning.value = false
  }
}

async function doAppend() {
  const body = { op: 'append', data_b64: toB64(appendText.value) }
  writeRunning.value = true
  writeTimings.value = null
  try {
    const { data } = await filesApi.patch(id.value, body)
    writeTimings.value = data.timings
    dropDeltaAnchor(id.value)
    ElMessage.success(`已追加 ${data.added_blocks} 个分块，共 ${data.block_count} 个分块`)
    appendText.value = ''
    await load()
  } catch {
  } finally {
    writeRunning.value = false
  }
}

async function doTruncate() {
  try {
    await ElMessageBox.confirm(
      `确认截断末尾 ${dropBlocks.value} 个分块。该操作不可撤销。`,
      '二次确认',
      { type: 'warning', confirmButtonText: '截断', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  const body = { op: 'truncate', drop_blocks: dropBlocks.value }
  writeRunning.value = true
  writeTimings.value = null
  try {
    const { data } = await filesApi.patch(id.value, body)
    writeTimings.value = data.timings
    dropDeltaAnchor(id.value)
    ElMessage.success(`已截断末尾 ${data.dropped_blocks} 个分块`)
    await load()
  } catch {
  } finally {
    writeRunning.value = false
  }
}

/**
 * 解密预览 —— **完全在浏览器里**：取密文 → 算分量 → 解封块密钥 → SM4 → 验证据。
 *
 * ★ 服务端在这条路上只提供**材料**（`/cipher`），不掌握任何秘密：
 *   私钥在 `stores/crypto` 的内存里，块密钥在本地解封。
 *   所以"明文旁边必须同时给出浏览器自己的验证结论"才算完整 ——
 *   后端那句 `ok: true` 在这里不参与判定。
 */
async function doDecrypt() {
  const key = crypt.key
  if (!key) {
    ElMessage.warning('本机尚未持有私钥 —— 请重新登录一次（登录时以口令在本地解封）')
    return
  }
  // ★ 用到私钥就把**闲置计时**重置一次（安全审计 N3）：连续操作不会被中途
  //   锁掉，而绝对上限（2 小时）不延长。
  crypt.touch()
  // ★★ 这一步是**整份**取回（`indices = null`）：响应 ≈ 密文 × 2（hex）
  //    + 每块一份块密钥密文 + 固定项（审计 N3 实测：1 KB 块约 2.7 KB/块）。
  //    小文件无感；32 MB 的文件会外推到几十 MB，所以按钮上写明体积，
  //    超过阈值再问一次（用户 2026-10-07 选定：提示 + 二次确认，不改语义）。
  const estBytes = estimateCipherPackBytes(file.value?.total_bytes, file.value?.layout?.length)
  if (estBytes > CIPHER_PACK_WARN_BYTES) {
    try {
      await ElMessageBox.confirm(
        `本文件大小为 ${fmtBytes(file.value?.total_bytes || 0)}，整份取回密文约 ${fmtBytes(estBytes)}。` +
          '浏览器还需逐块解封块密钥、执行 SM4 解密并重算分量，耗时较长。',
        '整份取回：数据量与耗时提示',
        { confirmButtonText: '继续取回', cancelButtonText: '取消', type: 'warning' },
      )
    } catch {
      return // 用户点了取消
    }
  }
  decrypting.value = true
  decryptResult.value = null
  try {
    const { data: pack } = await filesApi.cipher(id.value, null)
    // ★ δ 钉扎（审计 S4）：本地有就**用它**验；没有才退回远端的那一份。
    //   ★★ 且必须核对归属 —— `fileId` 会被库复用（见 `getDeltaAnchorFor`）。
    const localDelta = getDeltaAnchorFor(id.value, file.value?.content_digest)
    // ★★ 群参数（信任根，审计 S1）：`openBlocks` 内部会读本地群参数锚并**强制核对** ——
    //    本地有锚、而远端换了 `N` 时会**直接抛错**，不做“换个 N 再验一遍”（那没有意义：
    //    服务端自选一个已知阶的 N 就能反解出让任何密文通过的证据）。
    const opened = openBlocks(pack, key, { trustedDelta: localDelta })
    // 首次见证：**验证通过才钉** —— 钉一个验不过的版本没有任何意义。
    let justAnchored = false
    const crsFp = crsFingerprint(pack.crs.N)
    if (!localDelta && opened.verify.ok) {
      // ★★ 系统参数（`N` / 群参数位长 / 素数基线）是**整套部署共用**的，
      //    用一份全局锚存（审计 S1 / P0-1）。必须与 δ **同时**钉：
      //    只钉 `(U, C)` 而不管 `N`，服务端换掉 `N` 就能让任意密文“验证通过 + δ 一致”。
      if (!getCrsAnchor()) {
        setCrsAnchor({
          N: pack.crs.N,
          l: pack.crs.l ?? null,
          prime_bits: pack.crs.prime_bits ?? null,
          // ★ 素数起点只在**老坐标**（有序全局表）下才有"基线"含义。
          //   新坐标是按块身份哈希派生的，它的 start 只是 2^{bits-1} 这个下界 ——
          //   把它当成整部署共用的基线钉下去，之后遇到老文件就会误报"换了考场"。
          prime_start: pack.primes?.ordered === false ? null : (pack.primes?.start ?? null),
          source: '浏览器解密（首次见证）',
        })
      }
      setDeltaAnchor(id.value, {
        U: pack.delta.U,
        C: pack.delta.C,
        n: pack.delta.n,
        fp: pack.delta.fp,
        source: '浏览器解密（首次见证）',
        N: pack.crs.N,
        crsFp,
        //: 这份锚属于哪一份内容 —— 防 `fileId` 被库复用后认错文件（见 anchor.js）。
        digest: file.value?.content_digest,
      })
      justAnchored = true
    }
    decryptResult.value = {
      hex: bytesToHex(opened.plain),
      bytes: opened.plain.length,
      verify: opened.verify,
      remoteVerify: opened.remoteVerify,
      deltaVerdict: opened.deltaVerdict,
      crsVerdict: opened.crsVerdict,
      crsFp,
      justAnchored,
      primeCheck: opened.primeCheck,
      delta: opened.delta,
      blocks: opened.blocks.length,
      elementAgrees: opened.blocks.every((b) => b.elementAgrees),
      // ★ 服务器这次有没有交回“旧版本”（回滚演示里打开的功能）：
      //   有的话，界面上要**单独说清**这是“回滚”，而不是“内容被改坏”。
      replayedBlocks: (pack.blocks || [])
        .filter((b) => b.replayed)
        .map((b) => b.block_idx),
      ms: opened.ms,
      timings: pack.timings,
    }
    if (!opened.verify.ok) {
      // ★★ 回滚 ≠ 数据被改坏。服务器交回旧版本时验证也会不过（旧密文算不出
      //    当前 δ），但那是**服务器在撑**，不是节点上的数据坏了 —— 不能说成
      //    同一句话，否则用户会去查错的方向（换节点 / 重新上传）。
      const rep = (pack.blocks || [])
        .filter((b) => b.replayed)
        .map((b) => b.block_idx)
      if (rep.length) {
        ElMessage.error({
          message:
            `服务器交回了第 ${humanList(rep).join('、')} 块的「旧版本」（改块前那一版）—— ` +
            '本地验证因此不通过。这是「回滚」，不是数据被改坏。',
          duration: 10000,
          showClose: true,
        })
      } else {
        ElMessage.error(`浏览器本地验证未通过：${opened.verify.message}`)
      }
    } else if (opened.deltaVerdict === 'mismatch') {
      ElMessage.warning('δ 与本地钉住的版本不一致 —— 内容对得上你见证的那一版，请看下面的说明')
    } else {
      ElMessage.success(`浏览器本地解密 + 证据验证通过（${opened.ms} ms）`)
    }
  } catch (e) {
    ElMessage.error(e?.message || '客户端解密失败')
  } finally {
    decrypting.value = false
  }
}

// ------------------------------------------------------------------ 回滚演示
// ★ 这一小块是**演示工具**，不是业务功能：把“服务器不可信”变成看得见的一幕。
//   打开后服务器**真的**对这块交回存下来的旧版本（后续 /cipher 里把密文换掉），
//   于是客户端自己算出的分量对不上当前基准 —— 验证不通过。
const replayIdx = ref(0)
const replayArmed = ref(null) // null = 没打开；数字 = 已对这块打开
const replayBusy = ref(false)

async function toggleReplay() {
  const on = replayArmed.value === null
  // ★ 打开前问一次（与清零 / 截断同一手法）：这是**故意让验证不通过**的开关，
  //   而且是发生在服务端的真改动（它真的会把旧版本交回）。
  if (on) {
    try {
      await ElMessageBox.confirm(
        `自此刻起，服务端将对第 ${humanIdx(replayIdx.value)} 个分块返回「当前版本」的旧密文。\n\n` +
          '服务端会先留存该分块的当前内容；此后你更新该分块并执行解密时，服务端将返回旧的密文，' +
          '于是浏览器重算的分量与当前基准不一致，验证不通过。\n\n' +
          '本功能用于演示「服务端不可信」，不会破坏数据；点击「关闭」即可恢复。',
        '开启回滚演示',
        { type: 'warning', confirmButtonText: '开启演示', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  replayBusy.value = true
  try {
    await filesApi.replay(id.value, replayIdx.value, on)
    replayArmed.value = on ? replayIdx.value : null
    ElMessage({
      type: on ? 'warning' : 'success',
      duration: 6000,
      message: on
        ? `自此刻起，服务端将对第 ${humanIdx(replayIdx.value)} 个分块返回「当前版本」的旧密文。请更新该分块后执行解密。`
        : '已恢复：服务端重新返回当前版本。',
    })
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e?.message || '演示开关切换失败')
  } finally {
    replayBusy.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <PageHeader :title="pageTitle">
      <el-tag v-if="showOps" type="warning" size="small" effect="plain" style="margin-right: 10px">
        更新模式 · 写入将改写密文
      </el-tag>
      <el-button @click="router.push(backTo)">返回</el-button>
    </PageHeader>

    <div v-if="error" class="panel"><p class="text-danger">{{ error }}</p></div>
    <template v-else-if="file">


      <!-- 摘要与参数：这份文件对外的「公开事实」，抄下来拿去核对就行。 -->
      <div class="panel mb-3">
        <div class="sec-title-row">
          <h4 class="sec-title">摘要与参数</h4>
          <LockTag :can-decrypt="file.can_decrypt" />
        </div>
        <div class="kv-grid">
          <div class="kv"><span class="k">文件</span><span class="v mono">{{ file.owner }} / {{ file.file_key }}</span></div>
          <div class="kv"><span class="k">大小</span><span class="v mono">{{ fmtBytes(file.total_bytes) }}</span></div>
          <div class="kv"><span class="k">块数 n</span><span class="v mono">{{ file.block_count }}</span></div>
          <div class="kv"><span class="k">段大小</span><span class="v mono">{{ file.segment_bytes }} B</span></div>
          <div class="kv"><span class="k">版本</span><span class="v mono">v{{ file.version }}</span></div>
          <div class="kv"><span class="k">创建时间</span><span class="v mono">{{ fmtTime(file.created_at) }}</span></div>
          <div class="kv"><span class="k">当前全局块数 δ_n</span><span class="v mono">{{ file.delta_n }}</span></div>
          <div class="kv">
            <span class="k">位置段（全局编号）</span>
            <span class="v mono">{{ span(file.indices) }}</span>
          </div>
          <!-- ★ δ 指纹是**逐文件**的：它由本文件承诺的 U / C 与长度决定，用来判定
               「这份文件的证据还行吗」。悬浮给出本文件承诺的完整 U / C。 -->
          <div class="kv kv-full">
            <span class="k">δ 指纹（本文件）</span>
            <button
              type="button"
              class="v mono fp-link"
              :disabled="!file.delta_U"
              title="点击查看完整承诺 U / C"
              @click="deltaDialogOpen = true"
            >{{ file.file_delta_fp }}</button>
          </div>
          <div class="kv kv-full">
            <span class="k">上传时摘要（SHA-256）</span><span class="v mono">{{ file.content_digest }}</span>
          </div>
        </div>
        <details class="note-collapse">
          <summary>说明</summary>
          <p class="note">
            δ_n 是全系统当前的聚合承诺向量长度，与单个文件无关；证据 π_I 与之绑定，
            因此任何一次上传都会使此前的证据失效；判断既有证据是否仍然有效应以 δ 指纹为准。
            上传的摘要是入库时刻整份明文的摘要，文件内容更新后不会变动；
            文件当前的版本应以版本号为准。
          </p>
          <p v-if="showOps" class="note">
            <b>更新</b>（替换指定分块的内容）、
            <b>置零</b>（将指定分块替换为等长的全 0 字节）、
            <b>追加</b>（在向量末尾增加分块）、
            <b>截断</b>（移除本文件位置段的末尾部分）。
            四种操作都会重新加密受影响的分块、使版本号递增 1，并记入审计。
          </p>
        </details>
      </div>

      <!-- 封装过程：这份文件从明文到证据实际走了哪几步（数字都是真的，不是示意图）。 -->
      <div class="panel mb-3">
        <h4 class="sec-title">封装过程</h4>
        <ol class="steps">
          <li><b>分块</b><span>明文按段大小切分为 {{ file.block_count }} 个分块，每个分块的长度不超过 {{ file.segment_bytes }} 字节。</span></li>
          <li><b>逐块加密</b><span>每个分块使用独立的随机块密钥（SM4）加密；块密钥再以所有者的 SM2 公钥封装，仅所有者可解封装。</span></li>
          <li><b>计算分量</b><span>每个分块在向量中占一个位置。分块是数据单位，分量是该分块在这个位置上对外公开的坐标值：公开分量 = SM3(该分块的密文)，由密文唯一确定。</span></li>
          <li><b>形成承诺</b><span>各分块的分量按其身份映射到素数，聚合为 δ = (U, C) 两个群元素，与文件大小无关，恒不超过 256 字节。</span></li>
          <li><b>分散存放</b><span>{{ file.block_count }} 个分块按副本因子分布到各存储节点<span v-if="!showOps">（见下方块分布矩阵）</span>。</span></li>
          <li><b>按需生成证据</b><span>要证明指定分块集合仍然存在，即对集合 I 生成证据 π_I；验证仅使用 δ、π_I 与分块编号，无需明文。</span></li>
        </ol>
      </div>

      <div v-if="!showOps" class="panel mb-3">
        <h4 class="sec-title">块分布矩阵</h4>
        <BlockMatrix :nodes="nodes" :layout="file.layout" />
      </div>

      <!-- 块明细：每一块的**公开分量**就是它的指纹。 -->
      <div v-if="!showOps" class="panel mb-3">
        <div class="sec-head">
          <h4 class="sec-title" style="margin-bottom: 0">块明细（各分块的公开分量）</h4>
          <DetailToggle />
        </div>
        <BlockPreviewBar
          :total="(file.layout || []).length"
          :limit="BLOCK_PREVIEW_LIMIT"
          :expanded="blockPreviewOpen"
          @toggle="blockPreviewOpen = !blockPreviewOpen"
        />
        <el-table :data="blockRows" size="small" border max-height="360">
          <!-- ★ 块号显示的是 1 起（内部 0 起）：换算只在 utils/format.js 里做。 -->
          <el-table-column label="分块序号" width="80" align="center">
            <template #default="{ row }"><span class="mono">{{ humanIdx(row.block_idx) }}</span></template>
          </el-table-column>
          <!-- ★ 这一列是**存储槽位**（密文在节点上放哪），不是密码学坐标：
               改造后“第 i 块配哪个素数”由**块身份**派生（H(身份‖块号)），
               与槽位号毫无关系。所以它只在「详细」下露出，当调试信息用，
               标题里也明说是内部坐标 —— 别让人以为它是“全局下标”。 -->
          <el-table-column v-if="detailed" label="全局编号" width="150" align="center">
            <template #default="{ row }">
              <span
                class="mono"
                :title="`密文在存储节点上的槽位编号（内部值 ${row.global_index}，仅用于调试），与素数映射无关`"
              >{{ humanIdx(row.global_index) }}</span>
            </template>
          </el-table-column>
          <el-table-column v-if="detailed" label="分量指纹" min-width="240">
            <template #default="{ row }">
              <HashText v-if="row.element" :value="row.element" :len="24" />
              <span v-else class="text-3">—</span>
            </template>
          </el-table-column>
          <el-table-column label="明文长度" width="110" align="center">
            <template #default="{ row }"><span class="mono">{{ row.plain_len }} B</span></template>
          </el-table-column>
          <el-table-column label="存储节点（主副本）" min-width="230">
            <template #default="{ row }">
              <el-tag
                v-for="(r, i) in row.replicas || [row.holder]"
                :key="r"
                :type="i === 0 ? 'success' : 'info'"
                size="small"
                effect="plain"
                class="rep"
              >{{ r }}{{ i === 0 ? ' · 主副本' : '' }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
        <details class="note-collapse">
          <summary>说明</summary>
          <p class="note">
            本表按分块序号列出各分块的长度与副本所在节点。点击右上角「详细」可展开分量列，
            将指针悬停在分量上可查看完整值。
          </p>
        </details>
      </div>

      <!-- 这两块都是「对当前版本动手」的操作：左边把分块换回上一版本，
           右边演示服务端返回旧版本数据。并排一行，窄屏自动落回单列。 -->
      <div v-if="showOps && isMine" class="ops-row">
      <!-- 版本回退：把留过上一版的分块换回去，并重新承诺。
           只出现在「变更文件」形态、而且只有所有者看得到：它换回的是旧密文
           与旧块密钥（回退后重新生效），与更新是同一级别的权限。 -->
      <div class="panel">
        <div class="sec-head">
          <h4 class="sec-title" style="margin-bottom: 0">回退至上一版本</h4>
          <el-tag v-if="history.length" type="success" size="small" effect="plain">
            本文件有 {{ history.length }} 个分块可回退
          </el-tag>
          <el-tag v-else type="info" size="small" effect="plain">暂无可用上一版本</el-tag>
        </div>

        <p class="text-2" style="font-size: 13px; margin-top: 4px">
          每次更新或置零之前，该分块当时的完整本体（密文、IV、分量、块密钥密文、长度）
          会被自动留存一份，留存的是本体而非哈希。当节点数据丢失、或当前版本与基准不符时，
          将其替换为上一版本、重新计算分量并重新承诺，验证即可通过。
        </p>

        <el-alert
          v-if="!history.length"
          type="info"
          :closable="false"
          class="mb-2"
          title="当前没有可回退的版本"
          description="执行一次更新或置零后，被替换下来的版本才会成为「上一版本」。每个分块只保留一份（当前版本 + 上一版本）。"
        />

        <div v-else v-loading="historyLoading" class="rev-list">
          <div v-for="h in history" :key="h.block_idx" class="rev-item mono">
            <span class="rev-idx">第 {{ humanIdx(h.block_idx) }} 个分块</span>
            <span class="rev-ver">留存自第 {{ h.version }} 版</span>
            <span class="rev-size">{{ h.bytes }} B</span>
            <span class="text-3">{{ h.source }} · {{ fmtTime(h.created_at) }}</span>
          </div>
        </div>

        <div v-if="history.length" class="rev-actions">
          <el-button type="warning" :loading="restoring" @click="doRestore">
            将这 {{ history.length }} 个分块回退至上一版本
          </el-button>
          <span class="text-3" style="font-size: 13px">回退会重新计算聚合承诺，版本号递增 1（只增不减），并记入审计</span>
        </div>

        <el-alert
          v-if="restoreResult"
          type="success"
          :closable="false"
          class="mt-2"
          :title="`已回退 ${restoreResult.count} 个分块；当前为第 ${restoreResult.version} 版，聚合承诺已重算（本页数据已刷新）`"
        >
          <StageTimeline v-if="restoreResult.timings" :timings="restoreResult.timings" class="mt-2" />
        </el-alert>

        <details class="note-collapse">
          <summary>说明</summary>
          <p class="note">
            本功能针对一个具体情形：某一版本的数据在节点上无法取回（或与当前基准不符）。
            此时的处理目标是恢复文件的可用性 —— 将该分块替换为上一版本并重新承诺即可。
            回退不撤销历史：版本号只增不减，操作记入审计；被替换下来的版本会成为新的「上一版本」，
            因此可以反复回退。
          </p>
        </details>
      </div>

      <!-- 回滚演示：让服务端返回旧版本数据（演示「服务端不可信」）。
           只出现在「变更文件」形态、只有所有者看得到：非所有者既改不了分块，
           也就没有「服务端返回旧版本」这一幕可演。 -->
      <div class="panel">
        <div class="sec-head">
          <h4 class="sec-title" style="margin-bottom: 0">演示：服务端返回旧版本数据</h4>
          <el-tag type="warning" size="small" effect="plain">演示功能 · 会令验证不通过</el-tag>
        </div>

        <p class="text-2" style="font-size: 13px; margin-top: 4px">
          这个开关演示「服务端不可信」：开启后，服务端先留存该分块此刻的密文，
          此后你每一次取它，它都返回这份旧密文。浏览器自算的分量与当前基准对不上，
          本地验证随即失败 —— 也就是说，服务端即便偷偷换回旧数据，客户端也能自己发现。
        </p>

        <div class="demo-control">
          <span class="text-2">分块序号：</span>
          <el-input-number
            v-model="replayNoInput"
            :min="1"
            :max="Math.max(1, file?.block_count ?? 1)"
            size="small"
          />
          <el-button
            size="small"
            :type="replayArmed === null ? 'warning' : 'danger'"
            :loading="replayBusy"
            @click="toggleReplay"
          >
            {{ replayArmed === null ? '开启演示（留存当前版本）' : `关闭演示（当前已开启：第 ${humanIdx(replayArmed)} 个分块）` }}
          </el-button>
        </div>
        <p v-if="decryptResult?.replayedBlocks?.length" class="note text-danger" style="margin-bottom: 0">
          本次服务端返回的是<b>第 {{ humanList(decryptResult.replayedBlocks).join('、') }} 个分块</b>的<b>旧版本</b>（更新前的那一版），
          因此上方的本地验证未通过。该情形属于<b>版本回滚</b>，而非数据损坏。
        </p>

        <details class="note-collapse">
          <summary>演示步骤</summary>
          <ol class="demo-steps">
            <li>在此填入分块序号，点击「开启演示」：服务端记下该分块此刻的密文。</li>
            <li>到下方「更新」标签页改写这一块（内容随意），使密文与刚才记下的不同。</li>
            <li>点击「解密并本地验证」：会看到本地验证未通过，并指出服务端返回的是哪一版的旧数据。</li>
            <li>点击「关闭演示」即可恢复，服务端重新返回当前版本。</li>
          </ol>
        </details>
      </div>
      </div>

      <div v-if="showOps" class="panel mb-3">
        <el-alert
          v-if="!isMine"
          type="info"
          :closable="false"
          class="mb-2"
          title="当前账号不是该文件的所有者 —— 可读，不可写"
          description="块密钥以文件所有者的 SM2 公钥封装，其他主体（包括管理员）均无法解封装，因而无法修改。该限制来自密码学上的不可行性，与权限设置无关。"
        />
        <el-tabs v-else v-model="activeOp" class="ops-tabs">
          <el-tab-pane label="更新" name="modify">
            <div class="write-form">
              <div>
                <span class="text-2">分块序号：</span>
                <el-input-number
                  v-model="blockNoInput"
                  :min="1"
                  :max="Math.max(1, file.block_count || 1)"
                  @change="oldInfo = null"
                />
                <el-button :disabled="!isMine" :loading="oldLoading" @click="fetchOldContent">
                  取回当前内容并校验
                </el-button>
              </div>

              <!--
                ★ 先说「结论」，再说「内容」。
                  现在有两条独立依据：① **密码学** —— 密文与证据都在浏览器里验
                  （分量自己从密文算、对本地 δ 跑 add_back 链），服务端伪造不了；
                  ② **本地锚** —— 本浏览器写这块时留下的明文 SHA-256
                  （localStorage，见 utils/anchor.js），用于抓“换成别的内容再换回来”。
              -->
              <el-alert
                v-if="oldInfo"
                :type="oldInfo.verdict === 'match' ? 'success' : oldInfo.verdict === 'no-anchor' ? 'info' : 'error'"
                :closable="false"
                :title="oldInfo.verdict === 'match'
                  ? `与本地记录一致：第 ${humanIdx(oldInfo.blockIdx)} 个分块 · ${oldInfo.bytes} 字节 · SHA-256 ${oldInfo.sha256}（浏览器本地验证通过，${oldInfo.ms} ms）`
                  : oldInfo.verdict === 'stale'
                    ? `服务端返回的是更新前的旧数据 —— 第 ${humanIdx(oldInfo.blockIdx)} 个分块`
                    : oldInfo.verdict === 'mismatch'
                      ? `与本地记录不一致 —— 服务端返回的内容已改变（第 ${humanIdx(oldInfo.blockIdx)} 个分块）`
                      : oldInfo.verdict === 'verify-failed'
                        ? `浏览器本地验证未通过 —— 该分块不可信（第 ${humanIdx(oldInfo.blockIdx)} 个分块）`
                        : '未读取过该分块：本地无记录，无法判定服务端是否更换过其内容'"
              >
                <div style="font-size: 13px">
                  <p v-if="oldInfo.verdict === 'stale'" class="text-danger">
                    服务端返回的内容属于更新前的版本（{{ fmtWhen(oldInfo.staleFrom?.ts) }} ·
                    由「{{ oldInfo.staleFrom?.source || '本地记录' }}」记录，
                    第 {{ oldInfo.staleFrom?.version ?? '—' }} 版）。
                    该情形属于<b>版本回滚</b>，而非数据损坏。内容已填入下方输入框，并标记为旧版本。
                    <br />
                    <span class="text-3">
                      本地验证未通过的原因是旧密文与当前基准不符，与回滚判定同源。
                    </span>
                  </p>
                  <p v-if="oldInfo.verdict === 'verify-failed'" class="text-danger">
                    该分块<b>未通过浏览器本地的证据验证</b>（{{ oldInfo.verifyMsg }}），
                    即取回的密文与本地 δ 不一致：可能是节点上的数据被篡改，
                    也可能是服务端提供了错误的证据。内容已一并给出，
                    但<b>不可</b>作为可信内容使用。
                  </p>
                  <p v-else-if="oldInfo.verdict === 'mismatch'">
                    本地记录的内容摘要为
                    <span class="mono">{{ oldInfo.anchor.sha256 }}</span>
                    （{{ fmtWhen(oldInfo.anchor.ts) }} 由「{{ oldInfo.anchor.source }}」记下），
                    这次拿到的是 <span class="mono">{{ oldInfo.sha256 }}</span>。
                    可能的原因是服务端篡改或回滚，也可能是该分块此后被更新（例如在其他标签页中操作）。
                    新取回的内容已填入下方输入框，请自行判断。
                  </p>
                  <p v-else-if="oldInfo.verdict === 'no-anchor'">
                    本地未记录该分块的摘要（可能由其他客户端写入）。<b>当前密文已通过本地证据验证</b>，
                    交付字节未被替换，但无法判定其与初始写入版本是否一致；后续更新时将记录该分块的锚点。
                  </p>
                  <p v-else>
                    本地锚：{{ fmtWhen(oldInfo.anchor.ts) }} · 由「{{ oldInfo.anchor.source }}」记下
                    <span
                      v-if="oldInfo.anchor.version !== null && oldInfo.anchor.version !== file.version"
                      class="text-3"
                    >
                      （锚在第 {{ oldInfo.anchor.version }} 版，现在第 {{ file.version }} 版
                      —— 期间有别的改动，不影响这一块）
                    </span>
                    <br />
                    <span v-if="oldInfo.binary" class="text-3">
                      该分块不是合法 UTF-8（二进制），不支持直接编辑；下方给出前若干字节的十六进制表示。
                    </span>
                  </p>
                </div>
              </el-alert>
              <div
                v-if="oldInfo?.binary"
                class="mono text-3"
                style="font-size: 13px; word-break: break-all"
              >
                {{ oldInfo.hex.slice(0, 160) }}{{ oldInfo.hex.length > 160 ? '…' : '' }}
              </div>

              <el-input v-model="modifyForm.text" type="textarea" :rows="3" placeholder="新内容（将重新加密并更换块密钥）" />
              <el-button type="primary" :disabled="!isMine" :loading="writeRunning" @click="doModify">更新</el-button>
            </div>
            <details class="note-collapse">
              <summary>说明</summary>
              <p class="note">
                「取回当前内容」的结论基于<b>两条相互独立的依据</b>：① <b>密码学验证</b> ——
                密文与证据均在浏览器本地校验，服务端无法伪造；
                ② <b>本地锚</b> —— 本账户写入该分块时留存的明文摘要，保存于本地，服务端不可读写，
                可检出回滚至旧版本的情形。该机制的覆盖范围限于当前客户端的记录，跨客户端不互通。
              </p>
            </details>
          </el-tab-pane>
          <el-tab-pane label="置零" name="zero">
            <div class="write-form">
              <div>
                <span class="text-2">分块序号：</span>
                <el-input-number
                  v-model="blockNoInput"
                  :min="1"
                  :max="Math.max(1, file.block_count || 1)"
                  @change="oldInfo = null"
                />
              </div>
              <el-button type="warning" :disabled="!isMine" :loading="writeRunning" @click="doZero">置零</el-button>
            </div>
            <details class="note-collapse">
              <summary>说明</summary>
              <p class="note">
                「置零」将指定分块的内容替换为<strong>等长的全 0 字节</strong>：其操作类型为更新
                （<span class="mono">op = mod</span>）而非删除，因此分块仍然存在、下标不变、总块数不变，
                完整性校验仍可通过；全 0 内容仅在解封装后可见。
                若需将某分块从向量中移除，只能截断<strong>末尾</strong>位置段（见「截断」）。
              </p>
            </details>
          </el-tab-pane>
          <el-tab-pane label="追加" name="append">
            <div class="write-form">
              <el-input v-model="appendText" type="textarea" :rows="3" placeholder="待追加至末尾的内容" />
              <el-button type="primary" :disabled="!isMine" :loading="writeRunning" @click="doAppend">追加</el-button>
            </div>
            <details class="note-collapse">
              <summary>说明</summary>
              <p class="note">
                追加不会回填上一分块的剩余空间，因此追加后的分块划分与重新上传的结果不完全相同。
              </p>
            </details>
          </el-tab-pane>
          <el-tab-pane label="截断" name="truncate">
            <div class="write-form">
              <div>
                <span class="text-2">截断末尾分块数：</span>
                <el-input-number v-model="dropBlocks" :min="1" :max="Math.max(1, file.block_count - 1)" />
              </div>
              <el-button type="danger" :disabled="!isMine" :loading="writeRunning" @click="doTruncate">截断</el-button>
              <el-alert type="warning" :closable="false" class="trunc-warn" title="仅截断本文件位置段的末尾">
                <template #default>
                  <p style="font-size: 13px">① 当前方案下每份文件各占独立的位置段，因此截断仅移除本文件位置段的末尾部分，不影响其他文件；旧设计中「删除中间一份需连带删除其后全部」的限制已不再适用（若该文件本身不在全局末尾，服务端将返回 400）。</p>
                  <p style="font-size: 13px">② 不允许截断至零个分块（整份删除属于另一操作）。此操作不可撤销。</p>
                </template>
              </el-alert>
            </div>
          </el-tab-pane>
        </el-tabs>
        <StageTimeline v-if="writeTimings" :timings="writeTimings" class="mt-3" />
      </div>

      <div v-if="showOps" class="panel">
        <h4 class="sec-title">解密预览（浏览器本地解密与验证）</h4>
        <el-alert
          v-if="!file.can_decrypt"
          type="info"
          :closable="false"
          class="mb-2"
          title="当前账号不是该文件的所有者 —— 本地不具备可解封的密钥"
          description="块密钥由所有者的公钥封装，本机无法解封装。该限制来自密码学上的不可行性，与权限设置无关。"
        />
        <el-button
          type="primary"
          :disabled="!file.can_decrypt"
          :loading="decrypting"
          :title="`将取回整份密文（约 ${fmtBytes(
            estimateCipherPackBytes(file.total_bytes, file.layout?.length),
          )}），随后在本机逐块解封、解密并验证`"
          @click="doDecrypt"
        >解密并本地验证</el-button>
        <el-alert
          v-if="decryptResult"
          :type="decryptResult.verify.ok ? 'success' : 'error'"
          :closable="false"
          class="mt-3"
          :title="decryptResult.verify.ok
            ? `浏览器本地验证通过：${decryptResult.blocks} 个分块 · ${decryptResult.bytes} 字节 · ${decryptResult.ms} ms`
            : `浏览器本地验证未通过：${decryptResult.verify.codeName} —— ${decryptResult.verify.message}`"
        >
          <div style="font-size: 13px">
            <p>
              素数与映射：{{ decryptResult.primeCheck.message }}。
              分量由浏览器依据密文重算，并与服务端返回值比对，结果：
              {{ decryptResult.elementAgrees ? '一致' : '不一致（以本地计算值为准）' }}。
            </p>
            <p>
              δ：n = {{ decryptResult.delta.n }} · 指纹
              <span class="mono">{{ decryptResult.delta.fp }}</span>
            </p>
            <p>
              <b>δ 钉扎（本地锚定）</b>：
              <template v-if="decryptResult.deltaVerdict === 'match'">
                本地 δ 与服务端 δ 一致。
              </template>
              <template v-else-if="decryptResult.deltaVerdict === 'mismatch'">
                本地 δ 与服务端 δ 不一致。
                本地验证结论：{{ decryptResult.verify.ok ? '通过' : '不通过' }}；
                以服务端返回的 δ 复验的结果：「{{
                  decryptResult.remoteVerify && decryptResult.remoteVerify.ok ? '通过' : '不通过'
                }}」。
              </template>
              <template v-else>
                本地尚未记录该文件的 δ。本次验证使用服务端返回的 δ。
                <template v-if="decryptResult.justAnchored">
                  本次验证通过，当前版本已记录于本地；后续验证将使用本地记录。
                </template>
              </template>
            </p>
            <p v-if="decryptResult.deltaVerdict === 'mismatch'" class="text-danger">
              本地 δ 与服务端 δ 不属于同一版本，
              可能由文件被修改或服务端返回不同版本的 δ 所致。
              本地 δ 验证通过仅表示内容与本地记录一致；两项验证均未通过时按告警处理。
            </p>
          </div>
        </el-alert>
        <div v-if="decryptResult" class="mt-3">
          <div class="text-2 mono" style="font-size: 13px">{{ decryptResult.bytes }} 字节</div>
          <div v-if="decodePreview" class="decrypt-text">
            <el-alert
              v-if="decodePreview.head || decodePreview.tail"
              type="warning"
              :closable="false"
              class="mb-2"
              :title="`分块边界处存在不完整字符（head=${decodePreview.head}, tail=${decodePreview.tail}）`"
              description="更新该分块会破坏相邻分块边界处字符的完整性。"
            />
            <el-alert
              v-if="hasBadBytes(decryptResult.hex)"
              type="info"
              :closable="false"
              class="mb-2"
              title="内容包含无法解码的字节（可能不是纯文本）"
            />
            <pre class="plain">{{ decodePreview.text }}</pre>
          </div>
        </div>

        <details class="note-collapse">
          <summary>说明</summary>
          <p class="note">
            点击「解密并本地验证」，在浏览器本地解出文件内容并校验其完整性，结果输出在下方。
            全过程在本机完成，私钥不提交给服务端。
          </p>
        </details>
      </div>
    </template>

    <!-- δ 承诺弹框：指纹只给短摘要，完整值（U / C）在这里看，并可直接复制。 -->
    <el-dialog
      v-model="deltaDialogOpen"
      width="760px"
      top="8vh"
      class="delta-dialog"
      append-to-body
      title="文件承诺 δ = (U, C)"
    >
      <p class="dd-lead">
        这是本文件当前公开承诺的完整值，在库里只占两个群元素（合计不超过 256 字节）。
        界面上那个「δ 指纹」是它的短摘要 —— sha256("U:C:n") 的前 12 位，仅供快速比对；
        若要核对完整承诺，请以这里的 U 与 C 为准。
      </p>

      <div class="dd-item">
        <div class="dd-head">
          <span class="dd-k mono">U</span>
          <span class="dd-hint">承诺的第一分量（十进制）</span>
          <el-button link type="primary" size="small" @click="copyDelta('U')">复制 U</el-button>
        </div>
        <div class="dd-v mono">{{ file?.delta_U || '—' }}</div>
      </div>

      <div class="dd-item">
        <div class="dd-head">
          <span class="dd-k mono">C</span>
          <span class="dd-hint">承诺的第二分量（十进制）</span>
          <el-button link type="primary" size="small" @click="copyDelta('C')">复制 C</el-button>
        </div>
        <div class="dd-v mono">{{ file?.delta_C || '—' }}</div>
      </div>

      <div class="dd-foot">
        <span class="dd-hint">δ 指纹 = sha256("U:C:n") 的前 12 位</span>
        <span class="mono">{{ file?.file_delta_fp || '—' }}</span>
        <el-button link type="primary" size="small" @click="copyDelta('fp')">复制指纹</el-button>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.sec-title {
  font-size: 17px;
  font-weight: 600;
  color: var(--text-1);
  margin-bottom: 14px;
}
.sec-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.rep {
  margin-right: 6px;
}
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
}
/* 标题行：标题在左，紧随其右放状态标签（间距 10px，垂直居中）。 */
.sec-title-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 14px;
}
.sec-title-row .sec-title {
  margin-bottom: 0;
}
.digest {
  margin-top: 10px;
}
.write-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 560px;
}
.trunc-warn {
  max-width: 560px;
}
.plain {
  background: var(--bg-raised);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  padding: 12px;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-1);
}
.decrypt-text {
  max-width: 720px;
}
/* 摘要与参数：两列表格，长值（δ 指纹 / 摘要）整行铺开。 */
.kv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 8px 24px;
}
.kv {
  display: flex;
  gap: 8px;
  align-items: baseline;
  font-size: 13px;
  min-width: 0;
}
.kv-full { grid-column: 1 / -1; }
.kv .k {
  color: var(--text-3);
  white-space: nowrap;
}
.kv .v {
  color: var(--text-1);
  word-break: break-all;
}
/* 封装过程：一步一行，序号在左边。 */
.steps {
  margin: 0;
  padding-left: 20px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.steps li {
  color: var(--text-2);
  font-size: 13px;
  line-height: 1.65;
}
.steps li b {
  color: var(--text-1);
  font-weight: 500;
  margin-right: 8px;
}
/* 回退面板：一行一块 */
.rev-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-width: 720px;
  margin-top: 4px;
}
.rev-item {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  padding: 7px 10px;
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--bg-raised);
  font-size: 13px;
}
.rev-idx {
  color: var(--text-1);
}
.rev-ver {
  color: var(--accent);
}
.rev-size {
  color: var(--text-2);
}
.rev-actions {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 12px;
}

/* ---------------------------------------------------------------------------
   两件「对当前版本动手」的事并排成一行：左边回退、右边回滚演示。
   align-items: stretch 让两块等高，底部的「说明」用 margin-top: auto 压到底边，
   于是两栏的折叠说明处于同一水平线上，看起来是一排而不是错落的两块。
   窄屏（< 1180px）自动落回单列，免得两栏都被压到不可读。
--------------------------------------------------------------------------- */
.ops-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 16px;
  align-items: stretch;
  margin-bottom: 16px;
}
.ops-row > .panel { display: flex; flex-direction: column; }
.ops-row > .panel > .note-collapse { margin-top: auto; }
@media (max-width: 1180px) {
  .ops-row { grid-template-columns: minmax(0, 1fr); }
}

/* 演示面板：操作行与四步清单 */
.demo-control {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 10px;
}
.demo-steps {
  margin: 0;
  padding-left: 20px;
  font-size: 13px;
  line-height: 1.85;
  color: var(--text-3);
}
.demo-steps li { margin-bottom: 2px; }

/* δ 指纹：按钮外观做成链接，点开看完整承诺 U / C。
   选择器带上 .kv .v 是为了盖过 .kv .v 里定死的正文色（同权重时后者会赢）。 */
.kv .v.fp-link {
  border: none;
  background: none;
  padding: 0;
  font: inherit;
  color: var(--accent);
  cursor: pointer;
  border-bottom: 1px dashed color-mix(in srgb, var(--accent) 55%, transparent);
}
.kv .v.fp-link:disabled { color: var(--text-3); cursor: default; border-bottom-style: dotted; }
.kv .v.fp-link:focus-visible { outline: 2px solid color-mix(in srgb, var(--accent) 60%, transparent); outline-offset: 2px; }

/* 四个写操作（更新 / 追加 / 截断 / 置零）：做成小卡片，选中的那张拿出来。
   字号比正文大一号；悬停上浮、选中亮起并带光晕，替换掉默认的下划线指示器。 */
.ops-tabs :deep(.el-tabs__header) { margin-bottom: 14px; }
.ops-tabs :deep(.el-tabs__nav-wrap::after) { display: none; }
.ops-tabs :deep(.el-tabs__active-bar) { display: none; }
.ops-tabs :deep(.el-tabs__item) {
  height: 34px;
  line-height: 34px;
  /* ★ 必须带 !important：Element Plus 会把**首项**的左侧、**末项**的右侧内边距
     单独清零（原本是为了让下划线指示器贴边）。做成卡片之后这会让「更新」偏左、
     「截断」偏右 —— 那两条规则的选不中（首项实际不是 :first-child，
     active-bar 占了一位），所以只能整项盖掉。 */
  padding: 0 16px !important;
  margin-right: 8px;
  font-size: 15px;
  font-weight: 500;
  color: var(--text-2);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  background: var(--bg-raised);
  transition: transform 0.18s ease, color 0.18s ease, background 0.18s ease,
    border-color 0.18s ease, box-shadow 0.18s ease;
}
.ops-tabs :deep(.el-tabs__item:hover) {
  color: var(--text-1);
  border-color: color-mix(in srgb, var(--accent) 30%, var(--line));
  transform: translateY(-1px);
  box-shadow: 0 3px 10px rgba(0, 0, 0, 0.25);
}
.ops-tabs :deep(.el-tabs__item.is-active) {
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, var(--bg-raised));
  border-color: color-mix(in srgb, var(--accent) 45%, var(--line));
  box-shadow: 0 3px 14px color-mix(in srgb, var(--accent) 22%, transparent);
}
/* ★ Element Plus 会把**首项**的左侧与**末项**的右侧内边距清零（原本是为了让
   下划线指示器贴边）；改成卡片之后，这会让「更新」偏左、「截断」偏右 —— 已在
   上面用 !important 一并盖掉，这里不再需要伪类规则。 */
@media (prefers-reduced-motion: reduce) {
  .ops-tabs :deep(.el-tabs__item) { transition: none; }
  .ops-tabs :deep(.el-tabs__item:hover) { transform: none; }
}
</style>

<!--
  δ 承诺弹框：el-dialog 默认 teleport 到 body，scoped 样式够不着它，
  因此这部分用非 scoped 样式，统一以 .delta-dialog 作前缀。
-->
<style>
.delta-dialog { border-radius: 12px; }
.delta-dialog .el-dialog__body { padding: 16px 22px 20px; }
.dd-lead { margin: 0 0 14px; font-size: 13.5px; line-height: 1.75; color: var(--text-2); }
.dd-item {
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--bg-raised);
}
.dd-head { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; }
.dd-head .el-button { margin-left: auto; }
.dd-k { font-size: 14px; font-weight: 600; color: var(--accent); }
.dd-hint { font-size: 13px; color: var(--text-3); }
.dd-v {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 13px;
  line-height: 1.7;
  color: var(--text-1);
  word-break: break-all;
  max-height: 132px;
  overflow: auto;
}
.dd-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-top: 8px;
  border-top: 1px dashed var(--line);
}
.dd-foot .el-button { margin-left: auto; }
</style>
