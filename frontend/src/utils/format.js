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

/** 把十进制字符串转成十六进制指纹（默认前 8 位）。 */
export function hexFp(value, len = 8) {
  const s = String(value ?? '')
  if (!s) return '—'
  try {
    const hex = BigInt(s).toString(16)
    return hex.length <= len ? hex : hex.slice(0, len)
  } catch {
    // 不是整数（比如已经是字符串型摘要）就原样截断
    return s.slice(0, len)
  }
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
 * 验证结果的标题 —— 失败时必须说清是哪一层没过。
 * 两层各管一段：承诺层管「分量对不对」，块哈希层管「密文与分量是不是还对得上」。
 */
export function verifyFailTitle(d) {
  if (!d) return ''
  if (d.ok) return '验证通过'
  if (!d.verify || !d.verify.ok) {
    return `验证失败 · ${d.verify?.code_name || '未知环节'}`
  }
  return '验证失败 · 块哈希层（承诺层是过的）'
}

/** 失败时的副标题（人话），与 verifyFailTitle 配套。 */
export function verifyFailDetail(d) {
  if (!d) return ''
  if (d.ok) return d.verify?.message || ''
  // 整体失败：先看是不是承诺层自己没过；那才有环节名可报。
  if (d.verify && !d.verify.ok) return d.verify.message
  // 承诺层过了 —— 那是块哈希层抓的（密文被换、分量没动）。
  return '密文与它声明的分量对不上 —— 分量没动、内容被换过。这一层专门抓这种情况。'
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
  // 后端给的是 local time 的 isoformat（无时区后缀），直接显示即可
  return String(iso).replace('T', ' ')
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
