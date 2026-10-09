/**
 * SM3 摘要 —— 国标 GB/T 32905-2016。
 *
 * 为什么前端要自己实现一份
 * ------------------------
 * 「服务器不可信」这条要求只有在**浏览器能独立重算**时才成立。本方案里
 * 向量分量是 `v_i = SM3(密文段)`（见 `core/crypto.py::vector_element`），
 * 于是"交付的字节对不对得上承诺"这件事，浏览器必须自己从密文算一遍 ——
 * 不能用服务端给的 `element`（那份是服务端声称的）。
 *
 * 本实现与 Python `cryptography` 的 SM3 **逐字节相同**，
 * 由 `frontend/tests/crypto.test.js` 对着 `crypto-vectors.json` 断言。
 *
 * 实现要点（都是 SM3 规范里的硬要求，写错一位结果就全错）
 * --------------------------------------------------------
 * * 分组 512 位，字 32 位大端；
 * * 消息扩展 68 个字，压缩时还要用 `W'[j] = W[j] ^ W[j+4]`；
 * * 常量 `T_j` 在 `j ∈ [0,16)` 取 `0x79cc4519`、`j ∈ [16,64)` 取 `0x7a879d8a`，
 *   并且要**循环左移 j 位**（`j` 超过 31 时按 `j mod 32`）；
 * * 布尔函数 `FF_j` / `GG_j` 在 16 位处分界（前异或、后多数/选择）；
 * * 填充：`0x80` + 若干 `0x00` + **64 位大端比特长度**，补到 `≡ 56 (mod 64)`。
 */

const IV = new Uint32Array([
  0x7380166f, 0x4914b2b9, 0x172442d7, 0xda8a0600,
  0xa96f30bc, 0x163138aa, 0xe38dee4d, 0xb0fb0e4e,
])

/** `sm3()` 的链变量草稿（模块级复用，理由同上）。 */
const _state = new Uint32Array(8)
/** `sm3()` 的分组草稿（模块级复用，理由同上）。 */
const _block = new Uint32Array(16)

/** 32 位循环左移。`n` 为 0 时 `x >>> 32` 在 JS 里等于 `x >>> 0`，恰好正确。 */
const rotl = (x, n) => ((x << n) | (x >>> (32 - n))) >>> 0

/** 置换函数 P0 / P1。 */
const p0 = (x) => (x ^ rotl(x, 9) ^ rotl(x, 17)) >>> 0
const p1 = (x) => (x ^ rotl(x, 15) ^ rotl(x, 23)) >>> 0

/**
 * 消息扩展的草稿区 —— **模块级复用**。
 *
 * ★ 为什么可以共用：JS 单线程，而 `compress` 不递归、也不跨 `await`。
 *   每次新分配 132 个字（528 字节）看上去不多，但 PBKDF2 一轮要压几十万块，
 *   那点分配会变成实打实的 GC 压力（实测：提出来之后 20 万轮快了一大截）。
 */
const W = new Uint32Array(68)
const Wp = new Uint32Array(64)

/** 压缩函数 CF(V, B)。就地更新 `v`（长度 8）。 */
export function compress(v, block) {
  for (let i = 0; i < 16; i++) W[i] = block[i]
  for (let i = 16; i < 68; i++) {
    const x = (W[i - 16] ^ W[i - 9] ^ rotl(W[i - 3], 15)) >>> 0
    W[i] = (p1(x) ^ rotl(W[i - 13], 7) ^ W[i - 6]) >>> 0
  }
  for (let i = 0; i < 64; i++) Wp[i] = (W[i] ^ W[i + 4]) >>> 0

  let a = v[0], b = v[1], c = v[2], d = v[3]
  let e = v[4], f = v[5], g = v[6], h = v[7]

  for (let j = 0; j < 64; j++) {
    const T = j < 16 ? 0x79cc4519 : 0x7a879d8a
    // 三项相加可能到 3·2^32，远小于 2^53，用 `>>> 0` 取模 2^32 后再循环左移
    const ss1 = rotl((((rotl(a, 12) + e) >>> 0) + rotl(T, j % 32)) >>> 0, 7)
    const ss2 = (ss1 ^ rotl(a, 12)) >>> 0
    const ff = j < 16 ? (a ^ b ^ c) : ((a & b) | (a & c) | (b & c))
    const gg = j < 16 ? (e ^ f ^ g) : ((e & f) | (~e & g))
    const tt1 = ((ff >>> 0) + d + ss2 + Wp[j]) >>> 0
    const tt2 = ((gg >>> 0) + h + ss1 + W[j]) >>> 0

    d = c
    c = rotl(b, 9)
    b = a
    a = tt1
    h = g
    g = rotl(f, 19)
    f = e
    e = p0(tt2)
  }

  v[0] = (v[0] ^ a) >>> 0
  v[1] = (v[1] ^ b) >>> 0
  v[2] = (v[2] ^ c) >>> 0
  v[3] = (v[3] ^ d) >>> 0
  v[4] = (v[4] ^ e) >>> 0
  v[5] = (v[5] ^ f) >>> 0
  v[6] = (v[6] ^ g) >>> 0
  v[7] = (v[7] ^ h) >>> 0
}

/**
 * 从 IV 开始压**一块**，返回压完的 8 字链值（新建的数组）。
 *
 * ★ 只给 `pbkdf2.js` 的 HMAC 快路径用：HMAC 的 ipad/opad 那两块只与密钥有关，
 *   整轮 PBKDF2（几十万次 HMAC）里它们是同一个值 —— 算一次、一直复用。
 *   常规摘要请调 `sm3()`。
 *
 * @param {Uint32Array} block 16 个字（一块 512 位，大端）
 * @returns {Uint32Array} 8 个字的链值
 */
export function sm3StateAfter(block) {
  const v = new Uint32Array(8)
  v.set(IV)
  compress(v, block)
  return v
}

/**
 * 算 SM3 摘要。
 *
 * @param {Uint8Array} data 待摘要的字节
 * @returns {Uint8Array} 32 字节摘要
 */
export function sm3(data) {
  const bytes = data instanceof Uint8Array ? data : new Uint8Array(data)
  const bitLen = bytes.length * 8

  // 补到 64 的整数倍：`0x80` + k 个 `0x00` + 8 字节长度，且 k ≥ 0
  const total = (((bytes.length + 8) >> 6) + 1) << 6
  const padded = new Uint8Array(total)
  padded.set(bytes)
  padded[bytes.length] = 0x80

  const dv = new DataView(padded.buffer)
  dv.setUint32(total - 8, Math.floor(bitLen / 0x100000000))
  dv.setUint32(total - 4, bitLen >>> 0)

  // 链变量与分组用模块级草稿（同上，避免每次摘要都分配）
  const v = _state
  v.set(IV)
  const block = _block
  for (let off = 0; off < total; off += 64) {
    for (let i = 0; i < 16; i++) block[i] = dv.getUint32(off + i * 4)
    compress(v, block)
  }

  const out = new Uint8Array(32)
  const odv = new DataView(out.buffer)
  for (let i = 0; i < 8; i++) odv.setUint32(i * 4, v[i])
  return out
}

/** SM3 摘要的十六进制串（调试/展示用）。 */
export function sm3Hex(data) {
  return [...sm3(data)].map((b) => b.toString(16).padStart(2, '0')).join('')
}
