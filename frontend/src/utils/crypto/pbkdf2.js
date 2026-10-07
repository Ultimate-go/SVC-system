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
import { sm3 } from './sm3.js'

const BLOCK = 64

/** HMAC-SM3。 */
export function hmacSm3(key, msg) {
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
  const out = new Uint8Array(dkLen)
  const idx = new Uint8Array(4)
  let pos = 0
  for (let i = 1; pos < dkLen; i++) {
    new DataView(idx.buffer).setUint32(0, i)
    let u = hmacSm3(pw, concat(salt, idx))
    const acc = Uint8Array.from(u)
    for (let c = 1; c < iterations; c++) {
      u = hmacSm3(pw, u)
      for (let j = 0; j < acc.length; j++) acc[j] ^= u[j]
    }
    const n = Math.min(acc.length, dkLen - pos)
    out.set(acc.subarray(0, n), pos)
    pos += n
  }
  return out
}
