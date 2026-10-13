<script setup>
/**
 * 文件详情 / 更新文件（★ 2026-10-12 拆页面：同一个组件两种形态，靠路由 meta 区分）。
 *
 *   · files-detail        → 「文件详情」：只读 —— 摘要与参数 / 封装过程 /
 *                            块分布矩阵 / 块明细；
 *   · files-update-detail → 「更新文件」：在上面的基础上多出改块 / 清零 /
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
      deltaFp: data?.file?.delta_fp ?? null,
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
      ElMessage.warning('浏览器里还没有私钥 —— 请重新登录一次（登录时用口令在本地解封）')
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
            `服务端交回来的内容命中了你在${fmtWhen(hist.record.ts)}改过的那一版` +
            `（${hist.record.source || '本地记录'}，第 ${hist.record.version ?? '—'} 版）：` +
            '这是改块前的旧数据（回滚或缓存了旧版本），不是当前版本。' +
            '注意：本地只存了它的哈希（用来认出它），所以这里只能给结论，给不出版本本身。',
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
    // 回退面板要用它 —— 只在「更新文件」形态下拉（详情页不显示这块）。
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
      `把第 ${blocks.join('、')} 块换回它们上一版的本体，并重新计算承诺。\n\n` +
        '回退会产生新的版本号（不是把版本号改回去），并记一条审计。\n' +
        '当前这一版会被留成新的“上一版”，所以回退本身也可以再回退。',
      '回退到上一版',
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
    ElMessage.success(`已回退 ${data.count} 块，现在是第 ${data.version} 版（已重新承诺）`)
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
    await anchorBytes(idx, new TextEncoder().encode(text), data, '改块')
    // ★ δ 变了（是**你自己**改的）—— 把本地钉的那一份丢掉，
    //   让下一次解密变成"重新见证"，而不是默默把新 δ 收下。
    dropDeltaAnchor(id.value)
    oldInfo.value = null
    ElMessage.success(`已改第 ${humanIdx(idx)} 块，现在是第 ${data.version} 版（本地锚已更新）`)
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
      `第 ${humanIdx(idx)} 块内容将替换为等长全 0 字节${len ? `（${len} 字节）` : ''}。\n\n` +
        '块号、位置与总块数不变，完整性校验仍可通过。\n' +
        '块密钥将重新封装，长度保持不变。\n\n原内容不可恢复，版本号 +1。',
      '清零这一块',
      { type: 'warning', confirmButtonText: '清零', cancelButtonText: '取消' },
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
    await anchorBytes(idx, new Uint8Array(data.zeroed?.bytes ?? len ?? 0), data, '清零')
    dropDeltaAnchor(id.value)
    oldInfo.value = null
    ElMessage.success(
      `已清零第 ${humanIdx(idx)} 块（${data.zeroed?.bytes ?? '?'} 字节全 0，长度不变），现在是第 ${data.version} 版`,
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
    ElMessage.success(`已追加 ${data.added_blocks} 块，共 ${data.block_count} 块`)
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
      `确认删除末尾 ${dropBlocks.value} 块。该操作不可撤销。`,
      '二次确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
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
    ElMessage.success(`已删除末尾 ${data.dropped_blocks} 块`)
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
    ElMessage.warning('浏览器里还没有私钥 —— 请重新登录一次（登录时用口令在本地解封）')
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
        `这份文件 ${fmtBytes(file.value?.total_bytes || 0)}，整份取回密文大约 ${fmtBytes(estBytes)}。` +
          '浏览器还要逐块解封块密钥、SM4 解密并重算分量，可能要等很久。',
        '整份取回，量不小',
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
        `接下来服务器会对第 ${humanIdx(replayIdx.value)} 块交回「现在这一版」的旧密文。\n\n` +
          '服务端先存下这一块当前的内容，之后你改块、再解密时，它会把旧的交回来 —— ' +
          '于是浏览器自算的分量与当前基准对不上，验证不通过。\n\n' +
          '这是演示「服务器不可信」，不是把数据弄坏了；点「关闭」即可恢复。',
        '打开回滚演示',
        { type: 'warning', confirmButtonText: '打开演示', cancelButtonText: '取消' },
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
        ? `已让服务器从这一刻起对第 ${humanIdx(replayIdx.value)} 块交回「现在这一版」。现在去改这块，然后解密。`
        : '已恢复：服务器重新交回当前版本。',
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
        更新模式 · 会改写内容
      </el-tag>
      <el-button @click="router.push(backTo)">返回</el-button>
    </PageHeader>

    <div v-if="error" class="panel"><p class="text-danger">{{ error }}</p></div>
    <template v-else-if="file">
      <div class="panel mb-3">
        <div class="meta">
          <div class="meta-item"><span class="k">大小</span><span class="mono">{{ fmtBytes(file.total_bytes) }}</span></div>
          <div class="meta-item"><span class="k">块数</span><span class="mono">{{ file.block_count }}</span></div>
          <div class="meta-item"><span class="k">版本</span><span class="mono">{{ file.version }}</span></div>
          <div v-if="detailed" class="meta-item"><span class="k">位置（全局编号）</span><span class="mono">{{ span(file.indices) }}</span></div>
          <div class="meta-item"><span class="k">δ_n</span><span class="mono">{{ file.delta_n }}</span></div>
          <div class="meta-item"><span class="k">δ 指纹</span><span class="mono">{{ file.delta_fp }}</span></div>
          <div class="meta-item"><LockTag :can-decrypt="file.can_decrypt" /></div>
        </div>
        <div class="digest text-2" style="font-size: 13px">
          上传时摘要（SHA-256）：<span class="mono">{{ hexFp(file.content_digest, 16) }}</span>
          <span class="text-3">（它不随改块更新，协调者手里没有完整明文）</span>
        </div>
      </div>

      <!-- 摘要与参数：这份文件对外的「公开事实」，抄下来拿去核对就行。 -->
      <div class="panel mb-3">
        <h4 class="sec-title">摘要与参数</h4>
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
          <div class="kv kv-full"><span class="k">δ 指纹</span><span class="v mono">{{ file.delta_fp }}</span></div>
          <div class="kv kv-full">
            <span class="k">上传时摘要（SHA-256）</span><span class="v mono">{{ file.content_digest }}</span>
          </div>
        </div>
        <details class="note-collapse">
          <summary>说明</summary>
          <p class="note">
            δ_n 是「全系统」当前的块数，不是这一份文件的 —— 证据 π_I 与它绑定，
            所以任何一次上传都会让手里的旧证据失效（判「我那枚证据还有效吗」要看 δ 指纹）。
            上传时摘要是入库那一刻整份明文的指纹，改块之后不再更新（协调者手里没有完整明文，算不出新的），
            「这份文件现在第几版」要看版本号。
          </p>
        </details>
      </div>

      <!-- 封装过程：这份文件从明文到证据实际走了哪几步（数字都是真的，不是示意图）。 -->
      <div class="panel mb-3">
        <h4 class="sec-title">封装过程</h4>
        <ol class="steps">
          <li><b>切开</b><span>明文按段大小切成 {{ file.block_count }} 块（每块 ≤ {{ file.segment_bytes }} B）。</span></li>
          <li><b>逐块加密</b><span>每块一把随机块密钥（SM4）；块密钥再用所有者的 SM2 公钥封装，只有所有者解得开。</span></li>
          <li><b>算分量</b><span>每一块的公开分量 = SM3(整块密文)：它就是这一块对外的指纹，由密文唯一决定。</span></li>
          <li><b>形成承诺</b><span>分量按块身份映射到素数，聚合成 δ = (U, C) 两个群元素 —— 与文件大小无关，恒 ≤ 256 B。</span></li>
          <li><b>分散存放</b><span>{{ file.block_count }} 块按副本铺到各节点<span v-if="!showOps">（见下方块分布矩阵）</span>。</span></li>
          <li><b>按需取证</b><span>要证明某几块还在，就对指定块集取一份证据 π_I；验证只用 δ、π_I 与块编号，不需要明文。</span></li>
        </ol>
      </div>

      <div v-if="!showOps" class="panel mb-3">
        <h4 class="sec-title">块分布矩阵</h4>
        <BlockMatrix :nodes="nodes" :layout="file.layout" />
      </div>

      <!-- 块明细：每一块的**公开分量**就是它的指纹。 -->
      <div v-if="!showOps" class="panel mb-3">
        <div class="sec-head">
          <h4 class="sec-title" style="margin-bottom: 0">块明细（每块的公开分量）</h4>
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
          <el-table-column label="块号" width="70" align="center">
            <template #default="{ row }"><span class="mono">{{ humanIdx(row.block_idx) }}</span></template>
          </el-table-column>
          <!-- ★ 这一列是**存储槽位**（密文在节点上放哪），不是密码学坐标：
               改造后“第 i 块配哪个素数”由**块身份**派生（H(身份‖块号)），
               与槽位号毫无关系。所以它只在「详细」下露出，当调试信息用，
               标题里也明说是内部坐标 —— 别让人以为它是“全局下标”。 -->
          <el-table-column v-if="detailed" label="存储槽位（内部编号）" width="150" align="center">
            <template #default="{ row }">
              <span
                class="mono"
                :title="`密文在存储节点上的槽位编号（内部值 ${row.global_index}，调试用），与素数无关`"
              >{{ humanIdx(row.global_index) }}</span>
            </template>
          </el-table-column>
          <el-table-column v-if="detailed" label="分量指纹（十六进制 · 悬浮看完整）" min-width="240">
            <template #default="{ row }">
              <HashText v-if="row.element" :value="row.element" :len="24" />
              <span v-else class="text-3">—</span>
            </template>
          </el-table-column>
          <el-table-column label="明文长度" width="100" align="center">
            <template #default="{ row }"><span class="mono">{{ row.plain_len }} B</span></template>
          </el-table-column>
          <el-table-column label="存在哪几台（主副本在前）" min-width="210">
            <template #default="{ row }">
              <el-tag
                v-for="(r, i) in row.replicas || [row.holder]"
                :key="r"
                :type="i === 0 ? 'success' : 'info'"
                size="small"
                effect="plain"
                class="rep"
              >{{ r }}{{ i === 0 ? ' · 主' : '' }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
        <details class="note-collapse">
          <summary>说明</summary>
          <p class="note">
            本表按块号列出文件每一块的内容长度与存放节点。点右上角「详细」可展开查看每块指纹，
            鼠标停在指纹上会显示完整值。
          </p>
        </details>
      </div>

      <!-- 版本回退（★ 条目 6）：把留过上一版的块换回去，并重新承诺。
           ★ 只出现在「更新文件」形态、而且只有所有者看得到：它换回的是旧密文
             与旧块密钥（回退后就又生效了），与改块是同一级别的权限。 -->
      <div v-if="showOps && isMine" class="panel mb-3">
        <div class="sec-head">
          <h4 class="sec-title" style="margin-bottom: 0">回退到上一版</h4>
          <el-tag v-if="history.length" type="success" size="small" effect="plain">
            本文件有 {{ history.length }} 块可回退
          </el-tag>
          <el-tag v-else type="info" size="small" effect="plain">还没有留过上一版</el-tag>
        </div>

        <p class="text-2" style="font-size: 13px; margin-top: 4px">
          每次改块 / 清零之前，这一块当时的整套本体（密文、IV、分量、块密钥密文、长度）
          会被自动留一份 —— 留的是本体，不是哈希。节点丢了、或者当前这一版对不上时，
          把上一版换回去、重算分量、重新承诺，验证就又能过。
        </p>

        <el-alert
          v-if="!history.length"
          type="info"
          :closable="false"
          class="mb-2"
          title="现在没有可回退的版本"
          description="改块或清零一次之后，被换下来的那一版才会成为“上一版”。每块只留一份（当前 + 上一版）。"
        />

        <div v-else v-loading="historyLoading" class="rev-list">
          <div v-for="h in history" :key="h.block_idx" class="rev-item mono">
            <span class="rev-idx">第 {{ humanIdx(h.block_idx) }} 块</span>
            <span class="rev-ver">留的是第 {{ h.version }} 版</span>
            <span class="rev-size">{{ h.bytes }} B</span>
            <span class="text-3">{{ h.source }} · {{ fmtTime(h.created_at) }}</span>
          </div>
        </div>

        <div v-if="history.length" class="rev-actions">
          <el-button type="warning" :loading="restoring" @click="doRestore">
            回退这 {{ history.length }} 块到上一版
          </el-button>
          <span class="text-3" style="font-size: 13px">回退会重新承诺，版本号 +1（只增不减），并记审计</span>
        </div>

        <el-alert
          v-if="restoreResult"
          type="success"
          :closable="false"
          class="mt-2"
          :title="`已回退 ${restoreResult.count} 块 → 现在是第 ${restoreResult.version} 版，承诺已重算（详情已刷新）`"
        >
          <StageTimeline v-if="restoreResult.timings" :timings="restoreResult.timings" class="mt-2" />
        </el-alert>

        <details class="note-collapse">
          <summary>说明</summary>
          <p class="note">
            这件事解决的是一个很具体的情形：某一版的数据在节点上取不到了（或对不上当前基准），
            但你要的其实不是“揪出谁坏”，而是把文件恢复可用 —— 那就把上一版换回去、重新承诺。
            回退不是“撤销历史”：版本号只增不减，动作进审计，而且被换下来的那一版会留成新的上一版，
            所以来回回退都行。
          </p>
        </details>
      </div>

      <!-- 回滚演示：让服务器交回旧版本（演示“服务器不可信”）。
           ★ 只出现在「更新文件」形态，而且**只有所有者**看得到：非所有者既改不了块，
             也就没有“服务器交回旧版”这一幕可演。 -->
      <div v-if="showOps && isMine" class="panel mb-3">
        <div class="sec-head">
          <h4 class="sec-title" style="margin-bottom: 0">演示：服务器交回旧版数据</h4>
          <el-tag type="warning" size="small" effect="plain">演示功能 · 会让验证不通过</el-tag>
        </div>
        <div style="display: flex; align-items: center; gap: 12px; flex-wrap: wrap">
          <span class="text-2">第</span>
          <el-input-number
            v-model="replayNoInput"
            :min="1"
            :max="Math.max(1, file?.block_count ?? 1)"
            size="small"
          />
          <span class="text-2">块</span>
          <el-button
            size="small"
            :type="replayArmed === null ? 'warning' : 'danger'"
            :loading="replayBusy"
            @click="toggleReplay"
          >
            {{ replayArmed === null ? '打开（存下当前这一版）' : `关闭（当前已打开：第 ${humanIdx(replayArmed)} 块）` }}
          </el-button>
        </div>
        <p v-if="decryptResult?.replayedBlocks?.length" class="note text-danger" style="margin-bottom: 0">
          ★ 这次服务器交回了<b>第 {{ humanList(decryptResult.replayedBlocks).join('、') }} 块</b>的<b>旧版本</b>（改块之前那一版）——
          所以上面的本地验证不通过。这是<b>回滚</b>，不是“内容被改坏”。
        </p>

        <details class="note-collapse">
          <summary>说明</summary>
          <p class="note">
            输入块号并点「打开」记录当前版本，再到下方「改一块」中修改该块内容，最后执行解密，
            即可看到本地验证不通过。点「关闭」恢复。用于演示存储方返回旧版本数据的情形。
          </p>
        </details>
      </div>

      <div v-if="showOps" class="panel mb-3">
        <h4 class="sec-title">写操作（只有所有者能改）</h4>
        <el-alert
          v-if="!isMine"
          type="info"
          :closable="false"
          class="mb-2"
          title="这份文件不是你的 —— 可以看，但改不动"
          description="块密钥是拿文件所有者的公钥封的：其他人（以及管理员）都解不开，也就改不了。这不是权限设置，是密码学上的打不开。"
        />
        <el-tabs v-else v-model="activeOp">
          <el-tab-pane label="分块更新" name="modify">
            <div class="write-form">
              <div>
                <span class="text-2">第几块：</span>
                <el-input-number
                  v-model="blockNoInput"
                  :min="1"
                  :max="Math.max(1, file.block_count || 1)"
                  @change="oldInfo = null"
                />
                <el-button :disabled="!isMine" :loading="oldLoading" @click="fetchOldContent">
                  取回当前内容（并校验）
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
                  ? `与本地记录一致：第 ${humanIdx(oldInfo.blockIdx)} 块 · ${oldInfo.bytes} B · SHA-256 ${oldInfo.sha256.slice(0, 16)}…（浏览器本地验证通过，${oldInfo.ms} ms）`
                  : oldInfo.verdict === 'stale'
                    ? `服务器交回了「改块前」的旧数据 —— 第 ${humanIdx(oldInfo.blockIdx)} 块`
                    : oldInfo.verdict === 'mismatch'
                      ? `与本地记录不一致 —— 服务器返回的内容变了（第 ${humanIdx(oldInfo.blockIdx)} 块）`
                      : oldInfo.verdict === 'verify-failed'
                        ? `浏览器本地验证未通过 —— 这一块不可信（第 ${humanIdx(oldInfo.blockIdx)} 块）`
                        : '没读过这块：本地没记录，无法判断服务器有没有换过它'"
              >
                <div style="font-size: 13px">
                  <p v-if="oldInfo.verdict === 'stale'" class="text-danger">
                    服务端返回的内容为改块前版本（{{ fmtWhen(oldInfo.staleFrom?.ts) }} ·
                    由「{{ oldInfo.staleFrom?.source || '本地记录' }}」记下，
                    第 {{ oldInfo.staleFrom?.version ?? '—' }} 版）。
                    该情形属于<b>版本回滚</b>，非数据损坏。内容已填入下方输入框，标记为旧版本。
                    <br />
                    <span class="text-3">
                      本地验证不通过源于旧密文与当前基准不符，与回滚判定为同一原因。
                    </span>
                  </p>
                  <p v-if="oldInfo.verdict === 'verify-failed'" class="text-danger">
                    这一块<b>没通过浏览器本地的证据验证</b>（{{ oldInfo.verifyMsg }}）。
                    说明拿到的密文与本地 δ 对不上 —— 可能是节点上的数据被改过，
                    也可能是服务端给了错的证据。内容已经放出来了，
                    但请<b>不要</b>当作可信内容使用。
                  </p>
                  <p v-else-if="oldInfo.verdict === 'mismatch'">
                    本地记录的内容摘要为
                    <span class="mono">{{ oldInfo.anchor.sha256.slice(0, 16) }}…</span>
                    （{{ fmtWhen(oldInfo.anchor.ts) }} 由「{{ oldInfo.anchor.source }}」记下），
                    这次拿到的是 <span class="mono">{{ oldInfo.sha256.slice(0, 16) }}…</span>。
                    可能是服务器篡改/回滚，也可能是这块后来被改过（比如在别的标签页）。
                    新拿到的内容已放进下面的输入框，请自己判断。
                  </p>
                  <p v-else-if="oldInfo.verdict === 'no-anchor'">
                    本地未记录该块哈希（可能由其他设备写入）。<b>当前密文已通过本地证据验证</b>，
                    交付字节未被替换，但无法判定其与初始写入版本是否一致。后续改块将记录该块锚点。
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
                      这块不是合法 UTF-8（二进制），不直接编辑；下面给前若干字节的十六进制。
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

              <el-input v-model="modifyForm.text" type="textarea" :rows="3" placeholder="新内容（会重新加密、换密钥）" />
              <el-button type="primary" :disabled="!isMine" :loading="writeRunning" @click="doModify">改块</el-button>
              <el-button :disabled="!isMine" :loading="writeRunning" @click="doZero">清零</el-button>
            </div>
            <p class="text-3" style="font-size: 13px">
                「取回当前内容」有<b>两条独立依据</b>：① <b>密码学</b> ——
                密文与证据在浏览器本地校验，服务端无法伪造；
                ② <b>本地锚</b> —— 账户写入该块时留存的明文哈希，记录于本地，服务端不可读写，
                可检出回滚至旧版本的情形。覆盖范围限于当前设备记录，跨设备记录不互通。
            </p>
            <p class="text-3" style="font-size: 13px">
              「清零」把上面这个块号的内容换成<strong>等长的全 0 字节</strong>：它走的是改块
              （<span class="mono">op = mod</span>）而不是删除，所以块仍在、下标不变、总块数不变，
              完整性校验仍可通过；全 0 内容仅在解封后可见。
              想把某块从向量里真的去掉，只能删<strong>末尾</strong>那一段（见「截断」）。
            </p>
          </el-tab-pane>
          <el-tab-pane label="追加" name="append">
            <div class="write-form">
              <el-input v-model="appendText" type="textarea" :rows="3" placeholder="要追加到末尾的内容" />
              <el-button type="primary" :disabled="!isMine" :loading="writeRunning" @click="doAppend">追加</el-button>
              <p class="text-3" style="font-size: 13px">
                追加不回头填上一块的空位，所以追加后的切法和重新上传不完全一样。
              </p>
            </div>
          </el-tab-pane>
          <el-tab-pane label="截断" name="truncate">
            <div class="write-form">
              <div>
                <span class="text-2">删除末尾几块：</span>
                <el-input-number v-model="dropBlocks" :min="1" :max="Math.max(1, file.block_count - 1)" />
              </div>
              <el-button type="danger" :disabled="!isMine" :loading="writeRunning" @click="doTruncate">截断</el-button>
              <el-alert type="warning" :closable="false" class="trunc-warn" title="只删这份文件自己的末尾">
                <template #default>
                  <p style="font-size: 13px">① 新方案下每份文件各占自己的位置段，所以删的是这份文件自己的末尾，碰不到别的文件 —— 旧设计里“删中间一份要连后面一起删”的束缚已经不在了（那份文件本身没在全局末尾时会报 400）。</p>
                  <p style="font-size: 13px">② 不能删到一块不剩（整份删除是另一件事）。此操作不可撤销。</p>
                </template>
              </el-alert>
            </div>
          </el-tab-pane>
        </el-tabs>
        <StageTimeline v-if="writeTimings" :timings="writeTimings" class="mt-3" />
      </div>

      <div v-if="showOps" class="panel">
        <h4 class="sec-title">解密预览（浏览器本地解密 + 本地验证）</h4>
        <el-alert
          v-if="!file.can_decrypt"
          type="info"
          :closable="false"
          class="mb-2"
          title="这份文件不是你的 —— 本地也没有能开它的钥匙"
          description="块密钥由所有者的公钥封装，所以这里解不开（不是权限问题，是密码学上打不开）。"
        />
        <el-button
          type="primary"
          :disabled="!file.can_decrypt"
          :loading="decrypting"
          :title="`会把整份密文取回来（约 ${fmtBytes(
            estimateCipherPackBytes(file.total_bytes, file.layout?.length),
          )}）然后在本机逐块解封、解密、验证`"
          @click="doDecrypt"
        >解密并本地验证</el-button>
        <el-alert
          v-if="decryptResult"
          :type="decryptResult.verify.ok ? 'success' : 'error'"
          :closable="false"
          class="mt-3"
          :title="decryptResult.verify.ok
            ? `浏览器本地验证通过：${decryptResult.blocks} 块 · ${decryptResult.bytes} 字节 · ${decryptResult.ms} ms`
            : `浏览器本地验证未通过：${decryptResult.verify.codeName} —— ${decryptResult.verify.message}`"
        >
          <div style="font-size: 13px">
            <p>
              素数：{{ decryptResult.primeCheck.message }}。
              分量由浏览器依据密文重算，与服务端返回值比对：
              {{ decryptResult.elementAgrees ? '一致' : '不一致（以本地计算值为准）' }}。
            </p>
            <p>
              δ：n = {{ decryptResult.delta.n }} · 指纹
              <span class="mono">{{ decryptResult.delta.fp }}</span>
            </p>
            <p>
              <b>δ 钉扎</b>：
              <template v-if="decryptResult.deltaVerdict === 'match'">
                本地 δ 与服务端 δ 一致。
              </template>
              <template v-else-if="decryptResult.deltaVerdict === 'mismatch'">
                本地 δ 与服务端 δ 不一致。
                本地验证结论：{{ decryptResult.verify.ok ? '通过' : '不通过' }}；
                拿服务端那份 δ 再验一次的结果是「{{
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
              本地 δ 与服务端 δ 版本不一致。
              可能由文件修改或服务端返回不同版本的 δ 导致。
              本地 δ 验证通过表示内容与本地记录一致；两项均未通过时按告警处理。
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
              :title="`块边界有半截字符（head=${decodePreview.head}, tail=${decodePreview.tail}）`"
              description="改这一块会连带弄坏相邻那个汉字。"
            />
            <el-alert
              v-if="hasBadBytes(decryptResult.hex)"
              type="info"
              :closable="false"
              class="mb-2"
              title="内容里有读不出的字节（可能不是纯文本）"
            />
            <pre class="plain">{{ decodePreview.text }}</pre>
          </div>
        </div>

        <details class="note-collapse">
          <summary>说明</summary>
          <p class="note">
            点「解密并本地验证」，在浏览器本地把文件解出来并校验是否完整，解出的内容显示在下方。
            整个过程在本地完成，无需把密钥交给服务端。
          </p>
        </details>
      </div>
    </template>
  </div>
</template>

<style scoped>
.sec-title {
  font-size: 14px;
  font-weight: 500;
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
.meta-item {
  display: flex;
  gap: 8px;
  align-items: baseline;
  font-size: 13px;
}
.meta-item .k {
  color: var(--text-3);
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
</style>
