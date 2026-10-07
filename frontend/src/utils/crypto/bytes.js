/**
 * 字节小工具 —— 前端密码学那几件套共用的最小集合。
 *
 * 刻意不引入任何库：本项目前端原本是零密码学依赖的，这一层是"浏览器自己
 * 解密、自己验证"的落地，多一个依赖就多一处"它的实现和 Python 那边不一样"
 * 的可能。全部实现都由 `frontend/tests/crypto.test.js` 对着 Python 的
 * 权威输出逐字节断言。
 */

/** 十六进制串 → 字节。容忍空串。 */
export function hexToBytes(hex) {
  const s = String(hex ?? '')
  if (s.length % 2 !== 0) throw new Error(`十六进制串长度必须是偶数，收到 ${s.length}`)
  // ★ 严格：非法字符**必须抛错**，不能靠 `parseInt` 返回 `NaN`、再被
  //   ``Uint8Array`` 静默变成 0（安全审计 P11）。
  //
  //   以前那样做，``keywrap.js::field()`` 的形状校验会被绕过 ——
  //   错误要一直拖到 tag 不匹配才现形，而那时报的是“密文被改过”、
  //   指不到“你传了个非法的十六进制串”。语义与后端 ``bytes.fromhex`` 对齐。
  const out = new Uint8Array(s.length / 2)
  for (let i = 0; i < out.length; i++) {
    const pair = s.substr(i * 2, 2)
    if (!/^[0-9a-fA-F]{2}$/.test(pair)) {
      throw new Error(
        `不是合法的十六进制串：第 ${i * 2} 个字符处是 ${JSON.stringify(pair)}`,
      )
    }
    out[i] = parseInt(pair, 16)
  }
  return out
}

/** 字节 → 小写十六进制串。 */
export function bytesToHex(bytes) {
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('')
}

/** 拼接任意多个字节串。 */
export function concat(...parts) {
  let total = 0
  for (const p of parts) total += p.length
  const out = new Uint8Array(total)
  let off = 0
  for (const p of parts) {
    out.set(p, off)
    off += p.length
  }
  return out
}

/** 定长比较（不短路）。长度不同直接判否。 */
export function bytesEq(a, b) {
  if (a.length !== b.length) return false
  let diff = 0
  for (let i = 0; i < a.length; i++) diff |= a[i] ^ b[i]
  return diff === 0
}

/** UTF-8 编码。 */
export function utf8(s) {
  return new TextEncoder().encode(s)
}

/** 字节 → 大整数（大端）。 */
export function bytesToBigInt(bytes) {
  let out = 0n
  for (const b of bytes) out = (out << 8n) | BigInt(b)
  return out
}

/**
 * 大整数 → 定长字节（大端）。
 *
 * `core/keywrap.py::unwrap_private_key` 把标量按 **32 字节固定长度**写回，
 * 所以这里必须补前导零 —— 少了这一步，私钥数值相同但字节不同，
 * 会让"私钥"在两边不等。
 */
export function bigIntToBytes(x, length) {
  const out = new Uint8Array(length)
  let v = x
  for (let i = length - 1; i >= 0; i--) {
    out[i] = Number(v & 0xffn)
    v >>= 8n
  }
  if (v !== 0n) throw new Error('整数放不进指定的字节长度')
  return out
}
