/**
 * PBKDF2-HMAC-SM3 —— 把用户口令拉伸成"用户私钥密文"的 KEK。
 *
 * 与 `core/keywrap.py::_stretch` 等价：
 *
 * ```
 * KEK = hashlib.pbkdf2_hmac("sm3", password.utf8, salt, iterations, dklen=32)
 * ```
 *
 * 注意 Python 那边用的是 **SM3** 作 HMAC 的哈希，不是 SHA-1/SHA-256 ——
 * 换错哈希会得到一个同样长度、但完全不同的 KEK，表现为"口令永远不对"。
 *
 * HMAC 的分组长度按 **SM3 的 64 字节**（不是 SHA-256 的 64 也恰好一样，
 * 但这里写死成常量以免被误改）。
 */

import { concat, utf8 } from './bytes.js'
import { compress, sm3, sm3StateAfter } from './sm3.js'

const BLOCK = 64
/** HMAC-SM3 的输出长度（一个块的摘要）。 */
const HMAC_BYTES = 32
/** 快路径能吃的最大消息长度（再长就要多压一块，那种情况走通用实现）。 */
const FAST_MSG_MAX = 55

/**
 * HMAC-SM3（通用实现，任意长度消息）。
 *
 * ★ 短消息（≤ 55 字节）会走下面那条**密钥备好**的快路径 ——
 *   PBKDF2 的消息恰好只有 20（salt ‖ 序号）或 32（上一轮摘要）字节，
 *   正好全在快路径里。
 */
export function hmacSm3(key, msg) {
  const m = msg instanceof Uint8Array ? msg : new Uint8Array(msg)
  if (m.length <= FAST_MSG_MAX) return hmacPrepared(prepareHmac(key), m, new Uint8Array(HMAC_BYTES))
  return hmacSm3Generic(key, m)
}

/** 长消息的通用实现（逐字对应 RFC 2104，不做任何缓存）。 */
function hmacSm3Generic(key, msg) {
  let k = key
  if (k.length > BLOCK) k = sm3(k)
  const padded = new Uint8Array(BLOCK)
  padded.set(k)
  const ipad = new Uint8Array(BLOCK)
  const opad = new Uint8Array(BLOCK)
  for (let i = 0; i < BLOCK; i++) {
    ipad[i] = padded[i] ^ 0x36
    opad[i] = padded[i] ^ 0x5c
  }
  return sm3(concat(opad, sm3(concat(ipad, msg))))
}

/** 把 64 字节的密钥块异或出 ipad / opad 那两块（按大端拆成 16 个字）。 */
function keyBlockWords(kb, xor) {
  const w = new Uint32Array(16)
  for (let i = 0; i < 16; i++) {
    w[i] = (((kb[i * 4] ^ xor) << 24) |
      ((kb[i * 4 + 1] ^ xor) << 16) |
      ((kb[i * 4 + 2] ^ xor) << 8) |
      (kb[i * 4 + 3] ^ xor)) >>> 0
  }
  return w
}

/**
 * 把**密钥**备好：ipad / opad 两块压出来的中间状态，以及本次计算复用的草稿。
 *
 * ★ 为什么能这么省：HMAC 的这两块**只与密钥有关**。PBKDF2 一轮 20 万次 HMAC
 *   用的都是同一个口令，于是"每次重压这两块"是纯浪费 ——
 *   朴素写法每次 4 次压缩 + 若干临时数组，快路径每次只要 **2 次压缩、0 次分配**。
 *
 * ★ 草稿随本次计算创建（不是模块级）：`pbkdf2Sm3Async` 会跨 `await` 让出主线程，
 *   两次并发计算不能让彼此踩掉对方的中间数据。
 */
function prepareHmac(key) {
  const k = key.length > BLOCK ? sm3(key) : key
  const kb = new Uint8Array(BLOCK)
  kb.set(k)
  return {
    ipad: sm3StateAfter(keyBlockWords(kb, 0x36)),
    opad: sm3StateAfter(keyBlockWords(kb, 0x5c)),
    msgBlock: new Uint32Array(16),
    outBlock: new Uint32Array(16),
    state: new Uint32Array(8),
  }
}

/**
 * 把字节按**大端**装进字数组的第 `count` 个位置往后（调用方先 `fill(0)`）。
 *
 * ★ 这里不能图省事用 `new Uint8Array(words.buffer)` 直接写字节：
 *   SM3 的分组是"32 位**大端**字"，而 x86 上 Uint32Array 的元素是**小端**映射到内存的
 *   —— 走字节视图写进去，字节序就反了（踩过：块里第一个字变成 0x80636261）。
 */
function packBytes(w, bytes, count) {
  for (let i = 0; i < count; i++) {
    w[i >> 2] |= (bytes[i] & 0xff) << (24 - (i & 3) * 8)
  }
}

/** 内层要压的那一块：`(msg ‖ 0x80 ‖ 补零 ‖ 比特长度)`，长度含 ipad 那 64 字节。 */
function packInnerBlock(w, msg) {
  w.fill(0)
  packBytes(w, msg, msg.length)
  const at = msg.length
  w[at >> 2] |= 0x80 << (24 - (at & 3) * 8)
  w[15] = ((BLOCK + msg.length) * 8) >>> 0
}

/** 外层要压的那一块：`(内层摘要 ‖ 0x80 ‖ 补零 ‖ 比特长度)`，长度含 opad 那 64 字节。 */
function packOuterBlock(w, state) {
  w.fill(0)
  // 内层摘要的 8 个字直接就是这一块的前 8 个字（大端字序一致，不用转来转去）
  for (let i = 0; i < 8; i++) w[i] = state[i]
  w[8] = 0x80 << 24 // 32 字节后面紧跟 0x80（正好占满第 9 个字的最高字节）
  w[15] = ((BLOCK + HMAC_BYTES) * 8) >>> 0
}

/** 把 8 字状态按大端写成 32 字节。 */
function writeState(state, out) {
  for (let i = 0; i < 8; i++) {
    const w = state[i]
    out[i * 4] = w >>> 24
    out[i * 4 + 1] = (w >>> 16) & 0xff
    out[i * 4 + 2] = (w >>> 8) & 0xff
    out[i * 4 + 3] = w & 0xff
  }
}

/**
 * 用备好的密钥算一次 HMAC-SM3：`out ← HMAC(key, msg)`。
 *
 * 只支持 `msg.length ≤ 55`（内层与外层各一块就够，见 `FAST_MSG_MAX`）。
 *
 * ★ 允许 `out` 与 `msg` 是同一块内存：内层先把消息打包进草稿区、最后才写 `out`，
 *   所以就地覆盖是安全的（PBKDF2 正是这么用的，少一次拷贝）。
 *
 * @param {object} p `prepareHmac` 的产物
 * @param {Uint8Array} msg 消息（≤ 55 字节）
 * @param {Uint8Array} out 32 字节输出
 * @returns {Uint8Array} `out`
 */
function hmacPrepared(p, msg, out) {
  // ---- 内层：ipad 那一块的中间状态 → 再压"消息块" ----
  const mb = p.msgBlock
  packInnerBlock(mb, msg)
  const st = p.state
  st.set(p.ipad)
  compress(st, mb)

  // ---- 外层：opad 那一块的中间状态 → 再压"内层摘要块" ----
  const ob = p.outBlock
  packOuterBlock(ob, st)
  st.set(p.opad)
  compress(st, ob)

  writeState(st, out)
  return out
}

/**
 * PBKDF2（`DK = T_1 ‖ T_2 ‖ …`，每块 `T_i = U_1 ⊕ … ⊕ U_c`）。
 *
 * @param {string|Uint8Array} password 口令（字符串按 UTF-8 编码）
 * @param {Uint8Array} salt 盐
 * @param {number} iterations 迭代数（调用方必须夹住上限，见后端注释里的 DoS 说明）
 * @param {number} dkLen 输出字节数，本方案固定 32
 */
export function pbkdf2Sm3(password, salt, iterations, dkLen = 32) {
  const pw = typeof password === 'string' ? utf8(password) : password
  const mac = prepareHmac(pw)
  const out = new Uint8Array(dkLen)
  const idx = new Uint8Array(4)
  const u = new Uint8Array(HMAC_BYTES)
  let pos = 0
  for (let i = 1; pos < dkLen; i++) {
    new DataView(idx.buffer).setUint32(0, i)
    hmacPrepared(mac, concat(salt, idx), u)
    const acc = Uint8Array.from(u)
    for (let c = 1; c < iterations; c++) {
      // 就地读写同一块：hmacPrepared 先拷消息再写输出，安全
      hmacPrepared(mac, u, u)
      for (let j = 0; j < acc.length; j++) acc[j] ^= u[j]
    }
    const n = Math.min(acc.length, dkLen - pos)
    out.set(acc.subarray(0, n), pos)
    pos += n
  }
  return out
}

/* ===========================================================================
   分片异步版 —— 与上面那份**逐位等价**，只在算的过程中把主线程还回去。
   =========================================================================
   为什么需要它：登录时浏览器要跑 20 万轮 PBKDF2-HMAC-SM3。纯 JS 一口气算完
   要 2~3 秒，而这几秒里主线程被占满 —— 页面**一帧都画不出来**，
   于是登录页的“正在解封…”动画会当场冻住（实测：一次 2784ms 的长任务）。
   这里每算满 10ms 就让出一次，界面照常刷新，代价约 2%。

   ★ 让出主线程用的是 MessageChannel，不是 setTimeout(0)：
     浏览器对嵌套的 setTimeout 有 4ms 起跳（规范里的 clamp），
     让出 200 次就白等 800ms。
*/

/** 每隔多少次迭代看一眼时钟（只读一次时间，开销可忽略）。 */
const CLOCK_EVERY = 256
/** 一次让出主线程前，最多连续计算多少毫秒（≈ 每帧算一点，界面不卡）。 */
const SLICE_MS = 10

/**
 * 让出主线程一次，在**下一个宏任务**继续（没有 setTimeout 的 4ms clamp）。
 *
 * 每次现开一个 MessageChannel、用完即关 —— 这样并发调用不会互相踩。
 */
function yieldToEventLoop() {
  if (typeof MessageChannel !== 'function') return new Promise((r) => setTimeout(r, 0))
  return new Promise((resolve) => {
    const ch = new MessageChannel()
    ch.port1.onmessage = () => {
      ch.port1.close()
      resolve()
    }
    ch.port2.postMessage(0)
  })
}

/**
 * PBKDF2-HMAC-SM3 的**分片异步**实现：结果与 `pbkdf2Sm3` 逐位相同。
 *
 * @param {string|Uint8Array} password 口令（字符串按 UTF-8 编码）
 * @param {Uint8Array} salt 盐
 * @param {number} iterations 迭代数（上限由调用方夹住，同同步版）
 * @param {number} dkLen 输出字节数，本方案固定 32
 * @param {{onProgress?: (fraction: number) => void, sliceMs?: number}} [options]
 *        `onProgress` 收到 0→1 的**真实进度**（按已完成的迭代数算），
 *        界面可以直接拿它画进度条，不必编一个假的百分比。
 * @returns {Promise<Uint8Array>} 与同步版完全一致的密钥材料
 */
export async function pbkdf2Sm3Async(password, salt, iterations, dkLen = 32, options = {}) {
  const onProgress = typeof options.onProgress === 'function' ? options.onProgress : null
  const sliceMs = Number.isFinite(options.sliceMs) ? options.sliceMs : SLICE_MS
  const pw = typeof password === 'string' ? utf8(password) : password
  const mac = prepareHmac(pw)
  const out = new Uint8Array(dkLen)
  const idx = new Uint8Array(4)
  const blocks = Math.ceil(dkLen / 32)
  const total = (iterations - 1) * blocks || 1
  const u = new Uint8Array(HMAC_BYTES)
  let done = 0
  let pos = 0
  for (let i = 1; pos < dkLen; i++) {
    new DataView(idx.buffer).setUint32(0, i)
    hmacPrepared(mac, concat(salt, idx), u)
    const acc = Uint8Array.from(u)
    let lastYieldAt = Date.now()
    for (let c = 1; c < iterations; c++) {
      // 就地读写同一块：hmacPrepared 先拷消息再写输出，安全
      hmacPrepared(mac, u, u)
      for (let j = 0; j < acc.length; j++) acc[j] ^= u[j]
      done++
      if ((c & (CLOCK_EVERY - 1)) === 0 && Date.now() - lastYieldAt >= sliceMs) {
        if (onProgress) onProgress(Math.min(1, done / total))
        await yieldToEventLoop()
        lastYieldAt = Date.now()
      }
    }
    const n = Math.min(acc.length, dkLen - pos)
    out.set(acc.subarray(0, n), pos)
    pos += n
  }
  if (onProgress) onProgress(1)
  return out
}
