/**
 * 大整数与显示相关的工具。
 *
 * ★ 核心约定：所有群元素一律按十进制字符串传输。
 *   Python 的 json 能处理任意精度 int，但 JS 的 Number 只有 53 位有效精度
 *   —— 群元素（上千位）一过 Number 就丢精度。
 *
 *   所以这里做十六进制指纹时必须走 BigInt，绝不能写 (Number(s)).toString(16)。
 *
 * ★ 本模块是纯函数：不 import Vue、不碰 DOM，否则 Node 测试挂。
 */

/**
 * 十进制字符串 → 十六进制（内部走 BigInt，**绝不经过 Number**）。
 *
 * ★ 补成**偶数位**（按字节对齐）：256 位的分量偶尔会少一位 —— 那不是丢了数据，
 *   只是最高位那个半字节是 0、被 toString(16) 省掉了。补上它，界面上写
 *   "64 位十六进制"就与 l=256 真正对得上（否则偶尔会出现 63 位，看起来像被截了）。
 * ★ 短串（hexFp）与完整串（hexFull）都走这里，所以两者永远是**同一串的前缀**关系。
 */
function toHex(value) {
  const s = String(value ?? '')
  if (!s) return ''
  try {
    const hex = BigInt(s).toString(16)
    return hex.length % 2 ? '0' + hex : hex
  } catch {
    // 不是整数（比如已经是字符串型摘要）就原样返回，下面按字符串截
    return s
  }
}

/** 把十进制字符串转成十六进制指纹（默认前 8 位）。 */
export function hexFp(value, len = 8) {
  const hex = toHex(value)
  if (!hex) return '—'
  return hex.length <= len ? hex : hex.slice(0, len)
}

/** 十进制字符串 → **完整**十六进制（悬浮提示用）。
 *
 * ★ 与 hexFp 同一个坑：上千位的十进制一过 Number() 就丢精度，必须走 BigInt。
 * ★ 悬浮提示里给**完整十六进制**而不是完整十进制：十进制 300 多位在提示框里
 *   换不了行、一眼也看不出头尾；十六进制短得多，而且与界面上那截前缀是
 *   **同一个字符串**的前缀关系 —— "看到的前 8 位就是完整值的开头 8 位"，
 *   人能把两边对上号。
 */
export function hexFull(value) {
  return toHex(value)
}

/** 十进制字符串的前若干位，给 tooltip 用。 */
export function decHead(value, len = 24) {
  const s = String(value ?? '')
  if (s.length <= len) return s
  return s.slice(0, len) + `…（共 ${s.length} 位）`
}

export function fmtBytes(n) {
  const v = Number(n || 0)
  if (v < 1024) return `${v} B`
  if (v < 1024 * 1024) return `${(v / 1024).toFixed(1)} KB`
  return `${(v / 1024 / 1024).toFixed(2)} MB`
}

export function fmtIndices(indices) {
  if (!indices || !indices.length) return '—'
  return indices.join(', ')
}

/** 把下标集合压成紧凑区间描述，如 0-3, 8, 16-19（与后端 span() 一致）。 */
export function span(indices) {
  if (!indices || !indices.length) return '—'
  const idx = [...indices].sort((a, b) => a - b)
  const parts = []
  let lo = idx[0]
  let prev = idx[0]
  for (const x of idx.slice(1)) {
    if (x === prev + 1) {
      prev = x
      continue
    }
    parts.push(lo === prev ? `${lo}` : `${lo}-${prev}`)
    lo = prev = x
  }
  parts.push(lo === prev ? `${lo}` : `${lo}-${prev}`)
  return parts.join(', ')
}

/**
 * 验证结果的标题 —— 失败时必须带上环节名。
 *
 * 现在只剩**一个**结论（见 core/store.py 的模块说明）：整体失败必然就是
 * 承诺验证没过，所以环节名一定有，不用再分"哪一层"。
 */
export function verifyFailTitle(d) {
  if (!d) return ''
  if (d.ok) return '验证通过'
  if (d.verify && !d.verify.ok) {
    return `验证失败 · ${d.verify.code_name || '未知环节'}`
  }
  // 兜底（旧数据：结论说失败、承诺却说通过）—— 照样不能显示成「验证失败 · OK」。
  return '验证失败'
}

/** 失败时的副标题（人话），与 verifyFailTitle 配套。 */
export function verifyFailDetail(d) {
  if (!d) return ''
  if (d.ok) return d.verify?.message || ''
  if (d.verify && !d.verify.ok) return d.verify.message
  // 没有环节名可报（旧数据）：绝不搬「验证通过」来当副标题。
  return '没有环节名 —— 重新取一次证据再看。'
}

/** 十六进制的密文片段（后端给的是 hex 字符串）。 */
export function hexPreview(hex, bytes = 8) {
  const s = String(hex ?? '')
  if (s.length <= bytes * 2) return s
  return s.slice(0, bytes * 2) + '…'
}

/** hex 字符串 → 可读文本预览（尽量按 UTF-8 猜，失败就退回 hex）。 */
export function hexToText(hex) {
  const s = String(hex ?? '')
  if (!s || s.length % 2) return ''
  try {
    const bytes = new Uint8Array(s.length / 2)
    for (let i = 0; i < bytes.length; i++) {
      bytes[i] = parseInt(s.substr(i * 2, 2), 16)
    }
    return new TextDecoder('utf-8', { fatal: true }).decode(bytes)
  } catch {
    return ''
  }
}

export function fmtTime(iso) {
  if (!iso) return '—'
  // ★ 后端统一给 **UTC** 的 isoformat（无时区后缀 —— 见 backend/models.py:utcnow，
  //   以及 admin.py 里的 `r.ts.isoformat(timespec="seconds")`）。
  //   以前这里当成"local time"直接显示，于是东八区下**每条时间都少 8 小时**
  //   （审计流水上最明显：刚做的操作显示成 8 小时前）。
  //   修法：无时区后缀的串补 `Z` 让 Date 按 UTC 解析，再按浏览器本地时区格式化。
  const s = String(iso)
  const d = new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(s) ? s : s + 'Z')
  if (Number.isNaN(d.getTime())) return s.replace('T', ' ') // 认不出来就原样显示
  const p = (n) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}` +
    ` ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  )
}

/** 相对时间，给证据池卡片看新旧用（δ 会变，旧证据就不成立了）。 */
export function fmtAgo(ts) {
  const d = Date.now() - Number(ts || 0)
  if (!d || d < 0) return '—'
  if (d < 60_000) return '刚刚'
  if (d < 3_600_000) return `${Math.floor(d / 60_000)} 分钟前`
  if (d < 86_400_000) return `${Math.floor(d / 3_600_000)} 小时前`
  return `${Math.floor(d / 86_400_000)} 天前`
}

/**
 * 判断一段密文块解码出来是否含「坏字节」。
 *
 * 用于解密预览：区分「真的读不出文字」与「读得出、只是有几处残缺」。
 * 返回 true 表示存在解码不出的字节（被替换成 U+FFFD）。
 */
export function hasBadBytes(hex) {
  const s = String(hex ?? '')
  if (!s || s.length % 2) return true
  try {
    const bytes = new Uint8Array(s.length / 2)
    for (let i = 0; i < bytes.length; i++) {
      bytes[i] = parseInt(s.substr(i * 2, 2), 16)
    }
    const text = new TextDecoder('utf-8', { fatal: false }).decode(bytes)
    return text.includes('\uFFFD')
  } catch {
    return true
  }
}

/**
 * 把一段块密文（hex）解成可读文本，并报告块边界的「半截字符」。
 *
 * 返回 { text, head, tail }：
 *  - text  解码出的文本（残缺处用 U+FFFD 替换）
 *  - head  块**开头**有多少字节是上一块被切断的多字节字符的「尾巴」
 *          （UTF-8 continuation byte 的数量）
 *  - tail  块**末尾**有多少字节是「没拼完」的多字节序列
 *
 * head / tail 不为 0 时，改写这一块会连带把相邻那个汉字弄坏 —— 界面必须提示。
 */
export function decodeBlockHex(hex) {
  const s = String(hex ?? '')
  if (!s || s.length % 2) return { text: '', head: 0, tail: 0 }

  const bytes = new Uint8Array(s.length / 2)
  for (let i = 0; i < bytes.length; i++) {
    bytes[i] = parseInt(s.substr(i * 2, 2), 16)
  }

  // head：开头连续多少个 continuation byte（0b10xxxxxx）
  let head = 0
  while (head < bytes.length && (bytes[head] & 0xc0) === 0x80) head++

  // tail：从末尾往回，数一个「未完成序列」占了多少字节
  // （lead byte 0b110xxxxx 需要 1 个 continuation、0b1110xxxx 需要 2、0b11110xxx 需要 3）
  let tail = 0
  let i = bytes.length - 1
  while (i >= 0) {
    const b = bytes[i]
    if ((b & 0xc0) === 0x80) {
      // continuation byte：往回找它的 lead
      let lead = i - 1
      while (lead >= 0 && (bytes[lead] & 0xc0) === 0x80) lead--
      if (lead < 0) break
      const lb = bytes[lead]
      let need = 0
      if ((lb & 0xe0) === 0xc0) need = 1
      else if ((lb & 0xf0) === 0xe0) need = 2
      else if ((lb & 0xf8) === 0xf0) need = 3
      const have = i - lead
      if (have < need) {
        tail = i - lead + 1
      }
      break
    } else {
      // lead byte 在末尾：序列没拼完
      const lb = b
      let need = 0
      if ((lb & 0xe0) === 0xc0) need = 1
      else if ((lb & 0xf0) === 0xe0) need = 2
      else if ((lb & 0xf8) === 0xf0) need = 3
      if (need > 0) {
        // 看后面还有几个 continuation
        let have = 0
        let j = i + 1
        while (j < bytes.length && (bytes[j] & 0xc0) === 0x80) {
          have++
          j++
        }
        if (have < need) tail = have + 1
      }
      break
    }
  }

  const text = new TextDecoder('utf-8', { fatal: false }).decode(bytes)
  return { text, head, tail }
}

/**
 * 节点「本地自检」那一栏的判据。顺序很重要（旧实现写过反，别再犯）：
 *   1. 先看 unreachable（连不上）
 *   2. 再看 fresh（这台还没初始化）
 *   3. 最后才是 proved（本地视图过不过 svc.verify）
 *
 * 返回 { ok, label, tone }；tone ∈ 'ok' | 'warn' | 'danger' | 'muted'。
 */
export function nodeSelfCheck(row) {
  if (!row) return { ok: false, label: '—', tone: 'muted' }
  if (row.unreachable) return { ok: false, label: '连不上', tone: 'danger' }
  if (row.fresh) return { ok: false, label: '未初始化', tone: 'muted' }
  if (row.proved === true) return { ok: true, label: '视图合法', tone: 'ok' }
  if (row.proved === false) return { ok: false, label: '视图不合法', tone: 'danger' }
  return { ok: false, label: '未验证', tone: 'warn' }
}

/**
 * 估一下一次「整份取密文」（`/api/files/{id}/cipher`、`indices = null`）有多大。
 *
 * ★★ 为什么要有它：「试解密」「解密整份」都是**整份**取，而响应 ≈
 *   密文（hex，× 2）+ 每块一份块密钥密文 + 固定项。审计 N3 实测：
 *   64 块 × 1 KB → 174 KB；按上传上限 32 MB（自动档 128 块 × 256 KB）外推
 *   ≈ 65 MB —— 那种体积浏览器要憋很久，所以界面得**事先说出来**。
 *
 * 口径取那三组实测的上界（字节/块 2723 / 1682 / 1162 ⇒ 这里按每块 1 KB 算）：
 *   ``≈ 文件字节 × 2 + 块数 × 1 KB + 8 KB``
 * 这是**估算**，不承诺精确 —— 它只用来在按钮上写个量级、并在过大时拦一下。
 */
export function estimateCipherPackBytes(totalBytes, blockCount) {
  const n = Math.max(0, Number(totalBytes) || 0)
  const b = Math.max(0, Number(blockCount) || 0)
  return Math.round(n * 2 + b * 1024 + 8 * 1024)
}

/** 整份取密文时「该先问一句」的阈值（16 MB）。 */
export const CIPHER_PACK_WARN_BYTES = 16 * 1024 * 1024
