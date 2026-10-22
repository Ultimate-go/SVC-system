/**
 * 十六进制视图的数据构造 —— 把一整个明文的 hex 串按**分块**切开，再按 16 字节一行。
 *
 * ★ 为什么单独一个模块：详情页（解密预览）与上传文件页（行级解密）要显示的是
 *   **同一种图**，只是数据来源不同（一个来自 `/decrypt` 应答，一个来自整份取回后
 *   在浏览器里解出的明文）。切块口径只该有一份实现，否则偏移量、末块长度、
 *   回滚标记这些细节迟早会出现两套说法。
 *
 * ★ 为什么按 ``plain_len`` 切而不是固定 1 KB：最后一个分块通常不满一块，
 *   而分块大小是上传时定的，这里只信每块登记的长度。
 * ★★ 但**回滚演示**时得用“这次真实解出来多长”（``realLen``）：服务端交回的是
 *   旧版本那一块，它的长度与**当前**账目可以不同（比如 1024 → 22）。按当前账目
 *   切就会把旧那块截短、把剩下的字节顶到下一块去，整份视图错位。
 * ★ 累计偏移以**全文**为坐标（不是块内偏移），翻到第 N 块时能直接读出该字节在
 *   文件里的位置 —— 与替换页的序号口径一致（界面 1 起，偏移 0 起）。
 *
 * @param {object} args
 * @param {string} args.hex 明文字节的小写十六进制串（长度必须为偶数）
 * @param {Array<{block_idx:number, plain_len?:number}>} args.layout 分块账目
 * @param {number} [args.total] 明文总字节数；缺省取 `hex.length / 2`
 * @param {Map<number,number>|Array<{idx:number,len:number}>} [args.realLen]
 *   本次解出的**真实**块长（回滚演示时与账目不同）
 * @param {Set<number>|number[]} [args.replayed] 交回旧版本的分块号
 * @returns {Array<{idx:number,len:number,offsetHex:string,rows:Array,replayed:boolean,hue:number}>}
 */
export function buildHexBlocks({ hex, layout, total, realLen, replayed } = {}) {
  const rows = [...(layout || [])].sort((a, b) => a.block_idx - b.block_idx)
  if (!hex || !rows.length) return []
  const bytes = total ?? hex.length / 2
  const replayedSet = replayed instanceof Set ? replayed : new Set(replayed || [])
  const lenMap =
    realLen instanceof Map
      ? realLen
      : new Map((realLen || []).map((x) => [x.idx, x.len]))

  const out = []
  let cursor = 0
  rows.forEach((b, i) => {
    // ★ 最后一块吃掉剩余字节：万一长度与解出的明文有出入，
    //   也不能把尾巴露在视图外面。
    const own = lenMap.has(b.block_idx) ? lenMap.get(b.block_idx) : (b.plain_len || 0)
    const len = i === rows.length - 1 ? Math.max(own, bytes - cursor) : own
    const slice = hex.slice(cursor * 2, (cursor + len) * 2)
    const rws = []
    for (let off = 0; off < slice.length / 2; off += 16) {
      const lineHex = slice.slice(off * 2, (off + 16) * 2)
      const cells = []
      const ascii = []
      for (let j = 0; j + 1 < lineHex.length; j += 2) {
        const byte = parseInt(lineHex.slice(j, j + 2), 16)
        cells.push({ hex: lineHex.slice(j, j + 2).toUpperCase(), zero: byte === 0 })
        const printable = byte >= 0x20 && byte <= 0x7e
        ascii.push({ ch: printable ? String.fromCharCode(byte) : '.', dot: !printable })
      }
      rws.push({
        off,
        offHex: (cursor + off).toString(16).toUpperCase().padStart(8, '0'),
        cells,
        ascii,
      })
    }
    out.push({
      idx: b.block_idx,
      len,
      offsetHex: cursor.toString(16).toUpperCase().padStart(8, '0'),
      rows: rws,
      replayed: replayedSet.has(b.block_idx),
      // 相邻分块色相至少差 40°（黄金角推进），避免两块颜色看着一样。
      hue: (i * 47) % 360,
    })
    cursor += len
  })
  return out
}

/** `Uint8Array` / 普通数组 → 小写十六进制串。 */
export function bytesToHexLower(bytes) {
  const arr = bytes instanceof Uint8Array ? bytes : Uint8Array.from(bytes || [])
  let out = ''
  for (const b of arr) out += b.toString(16).padStart(2, '0')
  return out
}
