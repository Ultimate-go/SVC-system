<script setup>
/**
 * 文件详情 / 更新文件（★ 2026-10-12 拆页面：同一个组件两种形态，靠路由 meta 区分）。
 *
 *   · files-detail        → 「文件详情」：只读 —— 摘要与参数（含封装过程说明）/
 *                            分布矩阵 + 分量明细；
 *   · files-update-detail → 「更新文件」：在上面的基础上多出**三种写操作**
 *                            （替换 / 追加 / 删除；置零是替换里的等长全 0 特例）、
 *                            回滚演示与解密验证。
 *
 * ★ 为什么复用同一个组件而不是拷一份：这些能力读的是**同一份状态**
 *   （file / layout / δ 本地锚），拆成两个文件就会各拉一次详情、各维护一份锚 ——
 *   迟早分叉。用一个形态开关，改动只有一份代码。
 *
 * - 文件元信息 + 版本 + delta_n / 文件指纹。
 * - 分布矩阵（layout[].replicas 标主/副本），与分量明细并排一行（1:3）。
 * - **分量明细表**：每块的公开分量 —— 跟着「简略/详细」开关
 *   决定要不要铺出来；详细模式会把每块的分量一起要回来（`?elements=1`）。
 * - 替换 / 追加 / 删除（三种写操作都显示 timings；置零是替换页上的一个小按钮）。
 * - 解密预览（**浏览器本地解密 + 本地验证**；结果在十六进制大弹框里看，按分块底色区分）。
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
import { getAnchor, setAnchor, sha256Hex, fmtWhen, getDeltaAnchorFor, setDeltaAnchor, dropDeltaAnchor, getCrsAnchor, setCrsAnchor, dropCrsAnchor, crsFingerprint, matchHistory } from '../../../utils/anchor'
import PageHeader from '../../../components/common/PageHeader.vue'
import BlockPreviewBar from '../../../components/common/BlockPreviewBar.vue'
import DetailToggle from '../../../components/common/DetailToggle.vue'
import HashText from '../../../components/common/HashText.vue'
import BlockMatrix from '../../../components/chart/BlockMatrix.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'
import LockTag from '../../../components/security/LockTag.vue'
import HexViewer from '../../../components/security/HexViewer.vue'
import { buildHexBlocks } from '../../../utils/hexview.js'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const theme = useThemeStore()

const id = computed(() => route.params.id)

/** 更新形态（替换 / 追加 / 删除三种写操作 + 回滚演示 + 解密验证）开关，见文件头。 */
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
/**
 * 写操作的端到端耗时（毫秒）：从点下按钮到**这个请求返回**的墙钟差值。
 *
 * ★ 与上传那条线同一口径：读条量的是“这一次写操作”，用 performance.now()
 *   做差（单调时钟），不用定时器累加 —— 标题里的总耗时必须是准确值。
 */
const writeElapsed = ref(0)
function markWriteStart() {
  writeElapsed.value = 0
  return performance.now()
}
function markWriteEnd(t0) {
  if (t0) writeElapsed.value = Math.round(performance.now() - t0)
}

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

/** 解密结果的大弹框（十六进制视图，参考 010 Editor 的分块底色区分）。 */
const decryptOpen = ref(false)

/** 点「解密并本地验证」：先把弹框打开（显示出“正在解…”），再跑一遍本地解封。 */
async function openDecrypt() {
  // ★★ 先确认**真的**有私钥 —— 判据是 `crypt.key`，不是 `crypt.unlocked` 那个标记：
  //    刷新过标签页的情况先用 sessionStorage 里那份接回来（限 5 分钟窗口）。
  //    没有就不开弹框（否则会弹出一个写着“尚未解密”的空框，比不弹还迷惑）。
  if (!crypt.key) crypt.restore(crypt.username || '')
  if (!crypt.key) {
    ElMessage.warning({
      message:
        '本机当前没有私钥，无法解密：私钥只在登录时用口令在浏览器本地解封，' +
        '且只活在本标签页（闲置 15 分钟或关闭标签即失效）。请退出后重新登录一次再试。',
      duration: 9000,
      showClose: true,
    })
    return
  }
  // ★★ 回滚演示开着、而分块情况变了（演示期间追加 / 删除过，或那块已被删掉）：
  //    服务端留存的旧密文已不属于当前向量 —— 这时去解密只会得到一堆看不懂的错，
  //    所以在**打开弹框之前**就说清，并且不打开解密弹框（用户 2026-10-18）。
  if (replayStale.value) {
    const n = Number(file.value?.block_count ?? 0)
    ElMessage.warning({
      message:
        `分块数量已变化（开启演示时 ${replayArmedCount.value} 块，现在 ${n} 块）：` +
        '服务端留存的旧密文与当前向量不再对应，解密与验证都无法给出有效结论。' +
        '请先「关闭演示」，再重新开启（或撤销这次追加 / 删除）。',
      duration: 9000,
      showClose: true,
    })
    return
  }
  decryptOpen.value = true
  await doDecrypt()
}

const isMine = computed(() => file.value?.is_mine)

/**
 * 任何写操作成功之后，上一次的解密结论就作废了。
 *
 * ★ 它描述的是**旧内容**：留着显示会让人以为“这次改完还是这个结论”。
 *   顺带把十六进制弹框也关掉（里面的字节已经被改过了）。
 */
function dropDecryptResult() {
  decryptResult.value = null
  decryptOpen.value = false
}

/** 浏览器侧的私钥状态（私钥只活在内存，见 stores/crypto.js）。 */
const crypt = useCryptoStore()

const decodePreview = computed(() => {
  if (!decryptResult.value?.hex) return null
  return decodeBlockHex(decryptResult.value.hex)
})

/**
 * 十六进制视图的数据：交给 `utils/hexview.js` 统一切块（与上传文件页同一份实现）。
 *
 * ★ 为什么按 `plain_len` 切、回滚时为什么用 `blockLens`、偏移为什么是全文坐标，
 *   都在那个模块的头注释里 —— 这里只负责把它要的四样东西取出来。
 */
const hexBlocks = computed(() =>
  buildHexBlocks({
    hex: decryptResult.value?.hex,
    layout: file.value?.layout,
    total: decryptResult.value?.bytes,
    realLen: decryptResult.value?.blockLens,
    replayed: decryptResult.value?.replayedBlocks,
  }),
)

  // ---- 改块前“看旧内容”：明文 + **本地锚定校验** ----
  //
  // ★ 为什么不是“调个解密接口显示出来”就完了：这一版的解密在服务端做
  //   （前端没有 SM3/SM4，也没有私钥），所以“明文有没有被换”只能靠客户端
  //   自己的记录来判 —— 依据是本浏览器写这块时留下的明文 SHA-256。
  //   详见 utils/anchor.js 的长注释（证明什么、不证明什么）。
  const oldLoading = ref(false)
  const oldInfo = ref(null) // { blockIdx, bytes, sha256, hex, binary, anchor, verdict }

/**
 * 换操作就把上一步的展示清掉（用户 2026-10-18）。
 *
 * ★ 从「替换」切到「删除」时，上一步的耗时阶段条 / 取回内容 / 结论不该继续挂着：
 *   它们描述的是**另一次操作**，留在屏幕上会被当成这一次的结果。
 */
watch(activeOp, () => {
  writeTimings.value = null
  writeElapsed.value = 0
  oldInfo.value = null
})

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
    ElMessage.success(which === 'fp' ? '文件指纹已复制' : `承诺分量 ${which} 已复制`)
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
      // ★ 拦截器已经弹过后端那句中文（节点联系不上 / 400…），别再多弹一条
      //   “Request failed with status code 400”；这里只补纯前端异常。
      if (!e?.shown) ElMessage.error(e?.message || e?.response?.data?.detail || '取回失败')
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
    await syncReplayState()
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

/**
 * 把“回滚演示开没开”从**服务端**同步回来。
 *
 * ★★ 为什么必须有这一步（2026-10-18 实测）：开关是服务端的状态（`replay_blocks`
 *   表），而界面原来只记“我刚才点过没有”。于是刷新一次页面、或换一个标签页
 *   开过，界面就显示“未开启”，服务端却还在交回旧密文 —— 接着替换一次，
 *   解密就报“长度与账目不符”。那不是数据坏，是界面在说假话。
 */
async function syncReplayState() {
  try {
    const { data } = await filesApi.replayStatus(id.value)
    const list = (data?.blocks || []).map((b) => Number(b.block_idx))
    replaySyncFailed.value = false
    replayArmedList.value = list
    if (list.length) {
      replayArmed.value = list[0]
      replayIdx.value = list[0]
      // 服务端已经开着，但“开启时是多少块”没人记 —— 以当前块数当基线：
      // 之后的追加 / 删除才会被识别成“块数变了”。
      replayArmedCount.value = Number(file.value?.block_count ?? 0)
    } else {
      replayArmed.value = null
      replayArmedCount.value = null
    }
  } catch {
    // ★★ 查询失败（后端未重启 → 405；非所有者 → 403）**绝不能**把本地状态
    //    抹成“未开启”—— 那正是“点一下替换，演示开关自己关了”的直接原因。
    //    保持原样，并且把“这次没问成”记下来，好在面板上如实说明。
    replaySyncFailed.value = true
  }
}

async function doModify() {
  const idx = modifyForm.blockIdx
  const text = modifyForm.text
  const body = { op: 'modify', block_idx: idx, data_b64: toB64(text) }
  writeRunning.value = true
  writeTimings.value = null
  const t0 = markWriteStart()
  try {
    const { data } = await filesApi.patch(id.value, body)
    writeTimings.value = data.timings
    markWriteEnd(t0)
    // ★ 内容是我自己刚写进去的 —— 顺手把本地锚更新掉，下次看这块就有判据了。
    await anchorBytes(idx, new TextEncoder().encode(text), data, '替换')
    // ★ δ 变了（是**你自己**改的）—— 把本地钉的那一份丢掉，
    //   让下一次解密变成"重新见证"，而不是默默把新 δ 收下。
    dropDeltaAnchor(id.value)
    oldInfo.value = null
    ElMessage.success(`已替换第 ${humanIdx(idx)} 个分块；当前为第 ${data.version} 版`)
    dropDecryptResult()
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
  const t0 = markWriteStart()
  try {
    const { data } = await filesApi.zero(id.value, idx)
    writeTimings.value = data.timings
    markWriteEnd(t0)
    // ★ 清零后的内容是**等长的全 0 字节**（长度就是原 plain_len）—— 客户端知道它，
    //   所以同样锚得下来，下次看这块一样能判服务器有没有换。
    await anchorBytes(idx, new Uint8Array(data.zeroed?.bytes ?? len ?? 0), data, '置零')
    dropDeltaAnchor(id.value)
    oldInfo.value = null
    ElMessage.success(
      `已置零第 ${humanIdx(idx)} 个分块（${data.zeroed?.bytes ?? '?'} 字节全 0，长度不变）；当前为第 ${data.version} 版`,
    )
    dropDecryptResult()
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
  const t0 = markWriteStart()
  try {
    const { data } = await filesApi.patch(id.value, body)
    writeTimings.value = data.timings
    markWriteEnd(t0)
    dropDeltaAnchor(id.value)
    ElMessage.success(`已追加 ${data.added_blocks} 个分块，共 ${data.block_count} 个分块`)
    appendText.value = ''
    dropDecryptResult()
    await load()
  } catch {
  } finally {
    writeRunning.value = false
  }
}

async function doTruncate() {
  try {
    await ElMessageBox.confirm(
      `确认删除末尾 ${dropBlocks.value} 个分块。该操作不可撤销。`,
      '二次确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  const body = { op: 'truncate', drop_blocks: dropBlocks.value }
  writeRunning.value = true
  writeTimings.value = null
  const t0 = markWriteStart()
  try {
    const { data } = await filesApi.patch(id.value, body)
    writeTimings.value = data.timings
    markWriteEnd(t0)
    dropDeltaAnchor(id.value)
    ElMessage.success(`已删除末尾 ${data.dropped_blocks} 个分块`)
    dropDecryptResult()
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
      /**
       * 每一块**实际解出来**的长度（不一定是当前账目上的长度）。
       *
       * ★★ 为什么必须记：回滚演示里服务端交回的是**旧版本**那一块，它的长度
       *   与当前账目可以不同。十六进制视图如果按 `file.layout[].plain_len`（当前账目）
       *   切分，就会把旧那块截短、把余下的字节顶到下一块去 —— 整份视图从此错位。
       */
      blockLens: opened.blocks.map((b) => ({
        idx: b.blockIdx,
        len: b.plain.length,
        replayed: b.replayed === true,
      })),
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
    // ★★ 服务端会如实标出“哪几块交回的是旧版本”。如果进页面时那个 GET
    //    没问成（后端未重启 → 405），就以这里为准把开关状态接过来 ——
    //    至少能让人看到“确实开着”，并且能一键关掉。
    const rep0 = decryptResult.value.replayedBlocks || []
    if (rep0.length) {
      replayArmedList.value = rep0.slice()
      replayArmed.value = rep0[0]
      if (replayArmedCount.value == null) {
        replayArmedCount.value = Number(file.value?.block_count ?? 0)
      }
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
    // ★★ 2026-10-19：群参数与本地记录不一致 —— 说明这套部署被重建过。
    //   确认后丢掉本地群参数锚，按当前部署重新见证一次（否则这个浏览器永远解不开）。
    if (e?.code === 'crs-mismatch') {
      let ok = false
      try {
        await ElMessageBox.confirm(e.message, '群参数已变化', {
          confirmButtonText: '重新见证',
          cancelButtonText: '取消',
          type: 'warning',
        })
        ok = true
      } catch {
        ok = false
      }
      if (ok) {
        dropCrsAnchor()
        try {
          await doDecrypt()
        } catch {
          /* 下面统一提示 */
        }
      }
      return
    }
    // ★ 同上：取密文失败时后端那句中文已经由拦截器弹过了，这里只处理
    //   纯前端异常（块密钥解封失败、SM4 解不开）。
    if (!e?.shown) ElMessage.error(e?.message || '客户端解密失败')
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
/**
 * 开启时这份文件的分块数（快照）。
 *
 * ★ 为什么需要它：演示期间追加 / 删除仍然可以做完（用户 2026-10-18 确认要允许），
 *   但块数一变，服务端留存的那份旧密文就不再对应当前向量 —— 此时去解密
 *   只会得到一堆看不懂的错。所以拿它做一道闸：块数变了、或者那块已经被删掉了，
 *   就当场说清，并且**不打开解密弹框**。
 */
const replayArmedCount = ref(null)
/**
 * 服务端当前被留存的**全部**块号（可能有多个：以前开过、又没关干净的）。
 *
 * ★ 关闭演示时要**逐个关**：只关一个的话，其它的还在交回旧密文 ——
 *   界面显示“已关闭”、实际仍在回滚，又是一次“界面在说假话”。
 */
const replayArmedList = ref([])
/** 这次向服务端确认开关状态没成功（后端未重启 / 非所有者）—— 面板上如实标出。 */
const replaySyncFailed = ref(false)
const replayBusy = ref(false)

/** 演示已开启但分块情况变了 ⇒ 不能解密（打开弹框前拦）。 */
const replayStale = computed(() => {
  if (replayArmed.value === null) return false
  const n = Number(file.value?.block_count ?? 0)
  return replayArmedCount.value !== n || replayArmed.value >= n
})

async function toggleReplay() {
  const on = replayArmed.value === null
  // ★ 关闭时用**真正被开启的那个块号**（`replayArmed`），不是输入框里的值：
  //   演示期间输入框可能被改过，块也可能已经被删掉 —— 否则会出现“关不掉”。
  const target = on ? replayIdx.value : replayArmed.value
  // ★ 打开前问一次（与置零 / 删除同一手法）：这是**故意让验证不通过**的开关，
  //   而且是发生在服务端的真改动（它真的会把旧版本交回）。
  if (on) {
    try {
      await ElMessageBox.confirm(
        `自此刻起，服务端将对第 ${humanIdx(replayIdx.value)} 个分块返回「当前版本」的旧密文。\n\n` +
          '服务端会先留存该分块的当前内容；此后你替换该分块并执行解密时，服务端将返回旧的密文，' +
          '于是浏览器重算的分量与当前基准不一致，验证不通过。\n\n' +
          '演示期间请不要对这份文件做追加或删除：块数一变，留存的旧密文就对不上当前向量了。',
        '开启回滚演示',
        { type: 'warning', confirmButtonText: '开启演示', cancelButtonText: '取消' },
      )
    } catch {
      return
    }
  }
  replayBusy.value = true
  try {
    if (on) {
      await filesApi.replay(id.value, target, true)
      replayArmed.value = replayIdx.value
      replayArmedList.value = [replayIdx.value]
      replayArmedCount.value = Number(file.value?.block_count ?? 0)
    } else {
      // 逐个关：服务端可能有历史遗留的几块都开着（见 replayArmedList 的注释）。
      const all = replayArmedList.value.length ? replayArmedList.value : [target]
      for (const bi of all) {
        try {
          await filesApi.replay(id.value, bi, false)
        } catch {
          /* 那一块已经不在（演示期间删过）—— 后端已允许无条件关闭，失败也继续 */
        }
      }
      replayArmed.value = null
      replayArmedList.value = []
      replayArmedCount.value = null
    }
    ElMessage({
      type: on ? 'warning' : 'success',
      duration: 6000,
      message: on
        ? `自此刻起，服务端将对第 ${humanIdx(replayIdx.value)} 个分块返回「当前版本」的旧密文。请替换该分块后执行解密。`
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
          <div class="kv"><span class="k">文件持有者</span><span class="v mono">{{ file.owner }}</span></div>
          <div class="kv"><span class="k">文件标识</span><span class="v mono">{{ file.file_key }}</span></div>
          <div class="kv"><span class="k">大小</span><span class="v mono">{{ fmtBytes(file.total_bytes) }}</span></div>
          <div class="kv"><span class="k">块数 n</span><span class="v mono">{{ file.block_count }}</span></div>
          <div class="kv"><span class="k">分块大小</span><span class="v mono">{{ file.segment_bytes }} B</span></div>
          <div class="kv"><span class="k">版本</span><span class="v mono">v{{ file.version }}</span></div>
          <div class="kv"><span class="k">创建时间</span><span class="v mono">{{ fmtTime(file.created_at) }}</span></div>
          <!-- ★ 这里显示的是 ``store.n``（全系统在用块数），**不是**本文件的 n
               （本文件的 n 就在上面「块数 n」那一行）。它只用来描述系统规模。 -->
          <div class="kv"><span class="k">全系统在用块数</span><span class="v mono">{{ file.delta_n }}</span></div>
          <div class="kv">
            <span class="k">位置段（全局编号）</span>
            <span class="v mono">{{ span(file.indices) }}</span>
          </div>
          <!-- ★ 两个东西别再叫混（用户口径）：
               · δ = (U, C, n) —— 本文件的公开承诺，就这两个群元素加一个块数；
               · **文件指纹** —— δ 的短摘要 sha256(U, C, n)[:12]，界面只需这一串。
               悬浮/点开看完整承诺。 -->
          <div class="kv kv-full">
            <span class="k">文件指纹</span>
            <button
              type="button"
              class="v mono fp-link"
              :disabled="!file.delta_U"
              title="点击查看完整承诺 δ = (U, C, n)"
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
            <b>δ = (U, C, n)</b> 是本文件对外公开的承诺：<b>U</b> 记录位置集合（累加器），
            <b>C</b> 把每个位置的分量按它的权重连乘，<b>n</b> 是本文件的分块数。
            界面上那个<b>文件指纹</b>是它的短摘要 —— sha256(U, C, n) 的前 12 位，仅供快速比对；
            要核对完整承诺，点指纹看 U 与 C 的完整值。
            全系统在用块数是各文件的块数之和（删文件或截断留下的空洞不计入，位置编号永不回收），
            它描述系统规模，<b>不是</b>本文件的 n。
            判断本文件的既有证据是否仍然有效，应以<b>文件指纹</b>为准：
            它由本文件的 U / C 与其块数 n 决定，别的文件上传或改写都不改变它，
            因此不会使本文件的证据失效；使本文件证据失效的，只有本文件自己的写操作。
            上传的摘要是入库时刻整份明文的摘要，文件内容更新后不会变动；
            文件当前的版本应以版本号为准。
          </p>
          <!-- ★ 封装过程原来单独占一个面板；现在收进本来就该讲这件事的「说明」里 ——
               它是上面那几行参数的来源，不属于需要一直铺在屏上的东西。 -->
          <p class="note"><b>封装过程</b>：这份文件从明文到一份可验证的承诺，实际走了下面六步。</p>
          <ol class="steps">
            <li><b>分块</b><span>明文按分块大小切分为 {{ file.block_count }} 个分块，每个分块的长度不超过 {{ file.segment_bytes }} 字节。</span></li>
            <li><b>逐块加密</b><span>每个分块使用独立的随机块密钥（SM4）加密；块密钥再以所有者的 SM2 公钥封装，仅所有者可解封装。</span></li>
            <li><b>计算分量</b><span>每个分块在向量中占一个位置。分块是数据单位，分量是该分块在这个位置上对外公开的坐标值：公开分量 = SM3(该分块的密文)，由密文唯一确定。</span></li>
            <li><b>形成承诺</b><span>各分块的分量按其身份映射到素数，聚合为 δ = (U, C, n)：U 记位置集合、C 记加权连乘、n 为分块数，两个群元素合计不超过 256 字节，与文件大小无关。</span></li>
            <li><b>分散存放</b><span>{{ file.block_count }} 个分块按副本因子分布到各存储节点<span v-if="!showOps">（见下方分布矩阵）</span>。</span></li>
            <li><b>按需生成凭证</b><span>要验证指定分块集合仍然存在，即对集合 I 生成凭证 π_I；验证仅使用 δ、π_I 与分块编号，无需明文。</span></li>
          </ol>
          <p v-if="showOps" class="note">
            写操作共<b>三种</b>：<b>替换</b>（改写指定分块的内容；把内容写成等长的全 0 字节
            即「置零」，它是替换页上的一个按钮，不是独立操作）、<b>追加</b>（在向量末尾增加分块）、
            <b>删除</b>（移除本文件位置段的末尾若干分块）。
            三种操作都会重新加密受影响的分块、使版本号递增 1，并记入审计；
            分块序号与位置段只在追加 / 删除时改变，替换不改变向量的长度。
          </p>
        </details>
      </div>

      <!-- 分布矩阵（左，约 1/4）与分量明细（右，3/4）并排一行：
           矩阵只看「哪块在哪台」，格子很小，独占一行太浪费；两者又是同一件事的
           两个视角（矩阵看位置、明细看内容），排在一起对照着看最方便。 -->
      <div v-if="!showOps" class="matrix-row mb-3">
        <div class="panel">
          <h4 class="sec-title">分布矩阵</h4>
          <BlockMatrix :nodes="nodes" :layout="file.layout" />
        </div>

        <!-- 分量明细：每一块的**公开分量**就是它在这个位置上的坐标值。 -->
        <div class="panel">
          <div class="sec-head">
            <h4 class="sec-title" style="margin-bottom: 0">分量明细</h4>
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
          <p class="note">
            <b>分量</b>：<b>分量</b>是该分块<b>密文</b>的 SM3 摘要 —— 取 32 字节
            摘要按<b>大端</b>读成一个大整数（所以恒小于 2<sup>256</sup>）；它是这一块在向量
            这个位置上的坐标值，也是承诺 C 里唯一随内容变化的量。
            表中「<b>分量指纹</b>」只是它的十六进制写法（不足 64 位左补 0）截取前
            <b>24 位</b>；
            分量由密文唯一确定、任意方使用相同密文都能重新计算，所以它既能被验证方独立复算，
            也不会泄露明文。
          </p>
        </details>
        </div>
      </div>

      <!-- ★ 一行两块：左边是**三种写操作**（约 2/3），右边是「回滚演示」（约 1/3）。
           ★ 「回退至上一版本」已于 2026-10-18 整体删除（用户明确不要这个功能）。 -->
      <div v-if="showOps" class="ops-row">
      <!-- 回滚演示：让服务端返回旧版本数据（演示「服务端不可信」）。
           只有所有者看得到：非所有者既改不了分块，也就没这一幕可演。 -->
      <div v-if="isMine" class="panel ops-demo">
        <div class="sec-head">
          <h4 class="sec-title" style="margin-bottom: 0">演示：服务端返回旧版本数据</h4>
          <el-tag type="warning" size="small" effect="plain">仅用于演示</el-tag>
        </div>

        <p class="text-2" style="font-size: 13px; margin-top: 4px">
          若服务器返回旧版本数据，验证不通过，避免来自服务器的恶意攻击。
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
          本次服务端返回的是<b>第 {{ humanList(decryptResult.replayedBlocks).join('、') }} 个分块</b>的<b>旧版本</b>（替换前的那一版），
          因此上方的本地验证未通过。该情形属于<b>版本回滚</b>，而非数据损坏；
          旧版本与当前版本的内容长度可以不同，密码学检验不依赖两者一致。
        </p>
        <!-- ★ 演示开着但块数变了（追加 / 删除过）—— 当场说清，而不是等解密报错。 -->
        <p v-if="replayStale" class="note text-danger" style="margin: 6px 0 0">
          演示已开启，但分块情况已变化（开启时 {{ replayArmedCount }} 块，现在 {{ file?.block_count ?? 0 }} 块）：
          服务端留存的旧密文与当前向量不再对应，<b>此时无法解密验证</b>。请先关闭演示。
        </p>
        <!-- ★ 没能向服务端确认开关状态（后端未重启 / 非所有者）—— 别装作知道。 -->
        <p v-if="replaySyncFailed" class="note text-3" style="margin: 6px 0 0">
          未能向服务端确认演示开关状态（后端可能还没重启到新版），以上为本地记录。
        </p>

        <details class="note-collapse">
          <summary>演示步骤</summary>
          <ol class="demo-steps">
            <li>在此填入分块序号，点击「开启演示」：服务端记下该分块此刻的密文。</li>
            <li>到下方「替换」标签页改写这一块（内容随意），使密文与刚才记下的不同。</li>
            <li>点击「解密并本地验证」：会看到本地验证未通过，并指出服务端返回的是哪一版的旧数据。</li>
            <li>点击「关闭演示」即可恢复，服务端重新返回当前版本。</li>
          </ol>
        </details>
      </div>

      <div class="panel ops-main">
        <el-alert
          v-if="!isMine"
          type="info"
          :closable="false"
          class="mb-2"
          title="当前账号不是该文件的所有者 —— 可读，不可写"
          description="块密钥以文件所有者的 SM2 公钥封装，其他主体（包括管理员）均无法解封装，因而无法修改。该限制来自密码学上的不可行性，与权限设置无关。"
        />
        <el-tabs v-else v-model="activeOp" class="ops-tabs">
          <el-tab-pane label="替换" name="modify">
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
                <!-- ★ 置零不是第四种操作，而是替换的一个特例（等长全 0）——
                     所以它只是本页上的一个小按钮，点击弹框二次确认。 -->
                <el-button
                  type="warning"
                  plain
                  size="small"
                  style="margin-left: 24px"
                  :disabled="!isMine"
                  :loading="writeRunning"
                  title="把这一块的内容写成等长的全 0 字节（属于替换，不是删除）"
                  @click="doZero"
                >置零</el-button>
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
                :type="oldInfo.verdict === 'match' || oldInfo.verdict === 'no-anchor' ? 'success' : 'error'"
                :closable="false"
                :title="oldInfo.verdict === 'match'
                  ? `与本地记录一致：第 ${humanIdx(oldInfo.blockIdx)} 个分块 · ${oldInfo.bytes} 字节 · SHA-256 ${oldInfo.sha256}（浏览器本地验证通过，${oldInfo.ms} ms）`
                  : oldInfo.verdict === 'stale'
                    ? `服务端返回的是更新前的旧数据 —— 第 ${humanIdx(oldInfo.blockIdx)} 个分块`
                    : oldInfo.verdict === 'mismatch'
                      ? `与本地记录不一致 —— 服务端返回的内容已改变（第 ${humanIdx(oldInfo.blockIdx)} 个分块）`
                      : oldInfo.verdict === 'verify-failed'
                        ? `浏览器本地验证未通过 —— 该分块不可信（第 ${humanIdx(oldInfo.blockIdx)} 个分块）`
                        // ★ 本地没记录时**别再说“无法判定”**（用户 2026-10-18）：
                        //   密文与证据已经在浏览器里验过了，这一句才是本次真正确认过的东西。
                        : '当前密文已通过本地证据验证'"
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
              <el-button type="primary" :disabled="!isMine" :loading="writeRunning" @click="doModify">替换</el-button>
            </div>
            <details class="note-collapse">
              <summary>说明</summary>
              <p class="note">
                「取回当前内容」的结论基于<b>两条相互独立的依据</b>：① <b>密码学验证</b> ——
                密文与证据均在浏览器本地校验，服务端无法伪造；
                ② <b>本地验证</b> —— 账户写入该分块时留存的明文摘要，保存于本地，服务端不可读写，
                可检出回滚至旧版本的情形。该机制的覆盖范围限于当前客户端的记录，跨客户端不互通。
              </p>
              <p class="note">
                <b>置零</b>：把该分块的内容写成<b>等长的全 0 字节</b>。操作路径与替换完全一致，
                因此分块仍然存在、序号不变、总块数不变，完整性校验通过，只有持有者解封装后全 0 可见；
                版本号递增 1，且原内容不可恢复。要把分块从向量里移除，只能用「删除」，
                而且只能删本文件位置段的末尾。
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
          <el-tab-pane label="删除" name="truncate">
            <div class="write-form">
              <div>
                <span class="text-2">删除末尾分块数：</span>
                <el-input-number v-model="dropBlocks" :min="1" :max="Math.max(1, file.block_count - 1)" />
              </div>
              <el-button type="danger" :disabled="!isMine" :loading="writeRunning" @click="doTruncate">删除</el-button>
            </div>
            <!-- ★ 2026-10-18：两段警告文案改成可缩起的小字（用户要求）。 -->
            <details class="note-collapse">
              <summary>说明</summary>
              <p class="note"><b>仅删除本文件位置段的末尾</b></p>
              <p class="note">
                ① 每份文件各占一段独立的位置，删除只作用于本文件位置段的末尾：
                无法删除本文件中间部分的分块，因为需要重新编号和承诺。
              </p>
              <p class="note">② 不允许删除至零个分块。此操作不可撤销。</p>
            </details>
          </el-tab-pane>
        </el-tabs>
        <StageTimeline v-if="writeTimings" :timings="writeTimings" :total-ms="writeElapsed" class="mt-3" />
      </div>
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
        <!-- ★ 能解密但本机暂时没有私钥（刷新超时 / 换了标签页）：说清原因与下一步，
             而不是让人点下去只看到“没反应”。 -->
        <el-alert
          v-if="file.can_decrypt && !crypt.key"
          type="warning"
          :closable="false"
          class="mb-2"
          title="本机当前没有私钥，暂时无法解密"
          description="私钥只在登录时用口令在浏览器本地解封，且只活在本标签页（闲置 15 分钟或关闭标签即失效）。退出后重新登录一次即可。"
        />
        <el-button
          type="primary"
          :disabled="!file.can_decrypt"
          :loading="decrypting"
          :title="`将取回整份密文（约 ${fmtBytes(
            estimateCipherPackBytes(file.total_bytes, file.layout?.length),
          )}），随后在本机逐块解封、解密并验证`"
          @click="openDecrypt"
        >解密并本地验证</el-button>
        <el-alert
          v-if="decryptResult"
          :type="decryptResult.verify.ok ? 'success' : 'error'"
          :closable="false"
          class="mt-3"
          :title="decryptResult.verify.ok
            ? `本地验证结论：通过（${decryptResult.blocks} 个分块 · ${decryptResult.bytes} 字节 · ${decryptResult.ms} ms）`
            : `本地验证结论：未通过（${decryptResult.verify.codeName}：${decryptResult.verify.message}）`"
        >
          <div class="dv-body">
            <p>
              <b>素性检验与公开分量</b>：本次涉及的
              {{ decryptResult.primeCheck.checked }} 个位置素数均通过素性检验
              （<span class="mono">e_i = H(owner‖file_key‖i)</span>）；
              公开分量 <span class="mono">v_i</span> 由浏览器对取回的密文重新计算
              （<span class="mono">v_i = SM3(c_i)</span>），再与服务端返回值逐一比对，
              结果{{ decryptResult.elementAgrees ? '一致' : '不一致（此后的检验一律采用本地计算值）' }}。
            </p>
            <p>
              <b>文件承诺</b> <span class="mono">δ = (U, C, n)</span> 中
              <span class="mono">n = {{ decryptResult.delta.n }}</span>；
              承诺指纹为 <span class="mono">sha256(U, C, n)</span> 的前 12 位：
              <span class="mono">{{ decryptResult.delta.fp }}</span>
              —— 仅用于快速比对，完整的 <span class="mono">U</span> / <span class="mono">C</span>
              见「摘要与参数」中的承诺弹框。
            </p>
          </div>
        </el-alert>
        <div v-if="decryptResult" class="mt-3">
          <el-button type="primary" plain @click="decryptOpen = true">
            查看文件内容（十六进制视图）
          </el-button>
          <span class="text-2 mono" style="font-size: 13px; margin-left: 8px">
            {{ decryptResult.bytes }} 字节
          </span>
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

    <!-- 解密结果大弹框：参考 010 Editor 的十六进制视图（分块底色区分 + 偏移列 + ASCII 栏）。 -->
    <el-dialog
      v-model="decryptOpen"
      width="1120px"
      top="4vh"
      class="hex-dialog"
      append-to-body
      destroy-on-close
      :title="`解密结果 · ${file?.file_key || ''}`"
    >
      <template v-if="decryptResult">
        <div class="hx-verdict" :class="decryptResult.verify.ok ? 'ok' : 'bad'">
          <span class="hx-verdict-k">验证结果</span>
          <b>{{ decryptResult.verify.ok ? '通过' : '不通过' }}</b>
          <span class="hx-sep">·</span>
          <span>{{ decryptResult.blocks }} 个分块</span>
          <span class="hx-sep">·</span>
          <span>{{ decryptResult.bytes }} 字节</span>
          <span class="hx-sep">·</span>
          <span>{{ decryptResult.ms }} ms</span>
          <span class="hx-sep">·</span>
          <span class="mono">承诺 δ 指纹 {{ decryptResult.delta.fp }}</span>
        </div>
        <p v-if="!decryptResult.verify.ok" class="text-danger" style="font-size: 13px; margin: 6px 0 0">
          {{ decryptResult.verify.codeName }} —— {{ decryptResult.verify.message }}
        </p>
        <el-alert
          v-if="decodePreview && (decodePreview.head || decodePreview.tail)"
          type="warning"
          :closable="false"
          class="mt-2"
          :title="`分块边界处存在不完整字符（head=${decodePreview.head}, tail=${decodePreview.tail}）`"
          description="替换该分块会破坏相邻分块边界处字符的完整性。"
        />
        <el-alert
          v-if="hasBadBytes(decryptResult.hex)"
          type="info"
          :closable="false"
          class="mt-2"
          title="内容包含无法解码的字节（可能不是纯文本）"
          description="右侧 ASCII 栏里的「.」既可能是真的句点，也可能是不可打印字节 —— 请以十六进制栏为准。"
        />

        <!-- 分块十六进制视图：书签栏 + 每块一卡（与上传文件页共用同一组件）。 -->
        <HexViewer :blocks="hexBlocks" />

        <details class="note-collapse">
          <summary>说明</summary>
          <p class="note">
            明文字节全部由浏览器在本地解出（私钥不提交给服务端），按分块切开展示：块的<b>左色条与标题颜色</b>
            区分不同分块，<b>偏移</b>是全文中的字节位置，<b>灰掉的 00</b> 表示该字节为零。
            本次<b>只用了公开参数</b>完成验证：δ = (U, C, n) 与证据都可被任何人复算，不含任何秘密。
          </p>
        </details>
      </template>
      <el-empty v-else description="尚未解密" />
    </el-dialog>

    <!-- δ 承诺弹框：指纹只给短摘要，完整值（U / C）在这里看，并可直接复制。 -->
    <el-dialog
      v-model="deltaDialogOpen"
      width="760px"
      top="8vh"
      class="delta-dialog"
      append-to-body
      title="文件承诺 δ = (U, C, n)"
    >
      <p class="dd-lead">
        这是本文件当前公开承诺的完整值：<b>U</b> 记位置集合、<b>C</b> 记加权连乘、
        <b>n</b> 是本文件分块数，在库里只占两个群元素（合计不超过 256 字节）。
        界面上那个「文件指纹」是它的短摘要 —— sha256(U, C, n) 的前 12 位，仅供快速比对；
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
        <span class="dd-hint">文件指纹 = sha256(U, C, n) 的前 12 位</span>
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
/* 删除页的说明现在是可缩起的小字（见 .note-collapse），不再需要单独宽度限制。 */
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
/* 解密结论的正文：四段（素性检验 / 文件承诺 / 与本地基线比对 / 版本不一致告警）。 */
.dv-body {
  font-size: 13px;
  line-height: 1.75;
}
.dv-body p {
  margin: 0 0 6px;
}
.dv-body p:last-child {
  margin-bottom: 0;
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
/* 分布矩阵（左，约 1/4）与分量明细（右，3/4）并排一行。
   矩阵一格只有一个 12px 的点，占满一整行纯属浪费；窄屏落回单列，
   免得矩阵被压到只剩一条缝。 */
.matrix-row {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 3fr);
  gap: 16px;
  align-items: start;
}
/* 网格项默认 min-width:auto，内部表格一宽就顶出去 —— 显式放开，让它自己滚。 */
.matrix-row > .panel {
  min-width: 0;
}
@media (max-width: 1100px) {
  .matrix-row {
    grid-template-columns: minmax(0, 1fr);
  }
}
/* 一行两块：左「三种写操作」（2/3）+ 右「回滚演示」（1/3）。
   ★ DOM 里演示面板在前面（它原来独占一段），用 order 把它排到右边 ——
     不靠挪模板来调整位置，免得那张很长的卡片被搬错一层。
   ★ 窄屏落回单列，且把演示面板放到写操作之后（顺序不跳）。 */
.ops-row {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(0, 1fr);
  gap: 16px;
  /* ★ 两张卡片**等高**（上下边对齐）：一张是三种写操作，一张是回滚演示。
     以前用 start，演示卡矮一截、下边悬在半空；现在铺满整行高度，
     两卡内部的「说明」都靠 margin-top:auto 压到底边。 */
  align-items: stretch;
  margin-bottom: 16px;
}
.ops-main { order: 1; min-width: 0; }
.ops-demo { order: 2; min-width: 0; }
.ops-row > .panel { display: flex; flex-direction: column; }
.ops-row > .panel > .note-collapse { margin-top: auto; }
@media (max-width: 1180px) {
  .ops-row { grid-template-columns: minmax(0, 1fr); }
  .ops-demo { order: 3; }
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

/* 文件指纹：按钮外观做成链接，点开看完整承诺 U / C。
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

/* 三种写操作（替换 / 追加 / 删除）：做成小卡片，选中的那张拿出来。
   字号比正文大一号；悬停上浮、选中亮起并带光晕，替换掉默认的下划线指示器。
   ★ 置零不是第四个标签页，而是替换页上的一个小按钮（等长全 0 的替换）。 */
.ops-tabs :deep(.el-tabs__header) { margin-bottom: 14px; }
.ops-tabs :deep(.el-tabs__nav-wrap::after) { display: none; }
.ops-tabs :deep(.el-tabs__active-bar) { display: none; }
.ops-tabs :deep(.el-tabs__item) {
  height: 34px;
  line-height: 34px;
  /* ★ 必须带 !important：Element Plus 会把**首项**的左侧、**末项**的右侧内边距
     单独清零（原本是为了让下划线指示器贴边）。做成卡片之后这会让「替换」偏左、
     「删除」偏右 —— 那两条规则的选不中（首项实际不是 :first-child，
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
   下划线指示器贴边）；改成卡片之后，这会让「替换」偏左、「删除」偏右 —— 已在
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

<!--
  解密结果弹框（十六进制视图）：同样 teleport 到 body，所以也用非 scoped 样式。
  配色向 010 Editor 靠：深底、等宽字、分块左侧色条 + 标题同色，零字节变暗。
-->
<style>
.hex-dialog { border-radius: 12px; }
.hex-dialog .el-dialog__body { padding: 14px 20px 18px; }
.hex-dialog .el-dialog__title { font-family: ui-monospace, Consolas, monospace; }

.hx-verdict {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 13px;
  padding: 8px 12px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--bg-raised);
}
.hx-verdict.ok { background: rgba(35, 160, 110, 0.1); border-color: rgba(35, 160, 110, 0.45); color: #2f9e6f; }
.hx-verdict.bad { background: rgba(214, 69, 69, 0.1); border-color: rgba(214, 69, 69, 0.45); color: #c94a4a; }
.hx-verdict-k { font-size: 12.5px; opacity: 0.75; margin-right: 2px; }
.hx-sep { opacity: 0.35; }
</style>
