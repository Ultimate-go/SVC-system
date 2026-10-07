/**
 * SM2 曲线点运算 + ECDH + KDF —— 解开"块密钥密文"所需的全部底座。
 *
 * 为什么必须自己写
 * ----------------
 * 本项目的块密钥密文不是标准 SM2 密文，而是 `core/keywrap.py` 定义的
 * **自定义 ECIES 形状**：
 *
 * ```
 * z    = ECDH_x(临时私钥, 所有者公钥)          // 32 字节（x 坐标）
 * kenc = KDF(z, "vds/ecies/v1/enc", 16)
 * body = SM4-CTR(块密钥, kenc, iv)
 * tag  = KDF(z, "vds/ecies/v1/tag" ‖ R ‖ iv ‖ body, 32)
 * ```
 *
 * 标准 SM2 解密库（含 `sm-crypto`）解不开它 —— 所以这里把曲线运算、
 * ECDH、以及那个**非标准 KDF** 都按 Python 侧原样实现。
 *
 * KDF 的形状（这里最容易写错）
 * ---------------------------
 * `core/sm2.py::kdf` 是 `SM3(shared ‖ counter_be32 ‖ info)`，
 * 计数器**从 1 起、放在中间**。GM/T 0003 的示例把 counter 放在别处，
 * 所以"照标准写"会得到一个完全不同的密钥流。
 *
 * 坐标与序列化
 * ------------
 * 未压缩点 `0x04 ‖ x(32) ‖ y(32)`；反序列化时**必须校验点在曲线上**
 * （否则会中无效曲线攻击）。内部走 Jacobian 坐标，模逆只在整个标量乘的
 * 最后做一次 —— 与 `core/sm2.py` 的取舍相同（朴素仿射在浏览器里要把
 * 256 次模逆，实测慢一个数量级）。
 */

import { concat } from './bytes.js'
import { sm3 } from './sm3.js'

// ---- 曲线参数（GB/T 32918.5 推荐曲线）-------------------------------------
export const P = 0xfffffffeffffffffffffffffffffffffffffffff00000000ffffffffffffffffn
export const A = 0xfffffffeffffffffffffffffffffffffffffffff00000000fffffffffffffffcn
export const B = 0x28e9fa9e9d9f5e344d5a9e4bcf6509a7f39789f515ab8f92ddbcbd414d940e93n
export const N = 0xfffffffeffffffffffffffffffffffff7203df6b21c6052b53bbf40939d54123n
export const GX = 0x32c4ae2c1f1981195f9904466a39c9948fe30bbff2660be1715a4589334c74c7n
export const GY = 0xbc3736a2f4f6779c59bdcee36b692153d0a9877cc62a474002df32e52139f0a0n

/** 基点。 */
export const G = [GX, GY]

const COORD = 32
const POINT_BYTES = 1 + 2 * COORD

// ---- 模运算 ---------------------------------------------------------------

/** 取模到 `[0, m)`。JS 的 `%` 对负数返回负值，所以必须夹一次。 */
export function mod(x, m) {
  // ★ m = 0 时 JS 的取模会抛 `RangeError: Division by zero`（安全审计 P12）。
  //   服务端如果在 pack 里塞一个 "0" 当模数，那会以一句英文异常冒到界面上 ——
  //   这里当场把它变成一句能看懂的话。
  if (m === 0n) throw new Error('模数不能为 0（服务端给的公开参数不合法）')
  const r = x % m
  return r < 0n ? r + m : r
}

/** 模幂（平方-乘，指数为非负大整数）。 */
export function modPow(base, exp, m) {
  let result = 1n
  let b = mod(base, m)
  let e = exp
  while (e > 0n) {
    if (e & 1n) result = (result * b) % m
    b = (b * b) % m
    e >>= 1n
  }
  return result
}

/** 模逆（扩展欧几里得）。 */
export function modInverse(a, m) {
  let oldR = mod(a, m)
  let r = m
  let oldS = 1n
  let s = 0n
  while (r !== 0n) {
    const q = oldR / r
    ;[oldR, r] = [r, oldR - q * r]
    ;[oldS, s] = [s, oldS - q * s]
  }
  if (oldR !== 1n) throw new Error('这个数在模下不可逆')
  return mod(oldS, m)
}

// ---- 曲线判定与点运算 -----------------------------------------------------

/** `y² ≡ x³ + ax + b (mod p)`，坐标还要落在素域内。无穷远点（null）算在曲线上。 */
export function isOnCurve(pt) {
  if (pt === null) return true
  const [x, y] = pt
  if (x < 0n || x >= P || y < 0n || y >= P) return false
  return mod(y * y - (x * x * x + A * x + B), P) === 0n
}

const JAC_INF = [0n, 1n, 0n]

/** Jacobian 倍点，用 a ≡ −3 的专用公式（SM2 的 a 恰好满足）。 */
function jacDouble([x, y, z]) {
  if (z === 0n || y === 0n) return JAC_INF
  const delta = mod(z * z, P)
  const gamma = mod(y * y, P)
  const beta = mod(x * gamma, P)
  const alpha = mod(3n * mod(x - delta, P) * mod(x + delta, P), P)
  const x3 = mod(alpha * alpha - 8n * beta, P)
  const z3 = mod((y + z) * (y + z) - gamma - delta, P)
  const y3 = mod(alpha * mod(4n * beta - x3, P) - 8n * gamma * gamma, P)
  return [x3, y3, z3]
}

/** Jacobian 点加（通用公式）。 */
function jacAdd(p1, p2) {
  const [x1, y1, z1] = p1
  const [x2, y2, z2] = p2
  if (z1 === 0n) return p2
  if (z2 === 0n) return p1

  const z1z1 = mod(z1 * z1, P)
  const z2z2 = mod(z2 * z2, P)
  const u1 = mod(x1 * z2z2, P)
  const u2 = mod(x2 * z1z1, P)
  const s1 = mod(y1 * z2 * z2z2, P)
  const s2 = mod(y2 * z1 * z1z1, P)

  const h = mod(u2 - u1, P)
  const r = mod(2n * (s2 - s1), P)
  if (h === 0n) {
    // 同一个 x：要么同点（倍点），要么互为逆元（相加得无穷远点）
    return r === 0n ? jacDouble(p1) : JAC_INF
  }

  const i = mod(2n * h, P) ** 2n % P
  const j = mod(h * i, P)
  const v = mod(u1 * i, P)
  const x3 = mod(r * r - j - 2n * v, P)
  const y3 = mod(r * mod(v - x3, P) - 2n * s1 * j, P)
  const z3 = mod(mod((z1 + z2) * (z1 + z2), P) - z1z1 - z2z2, P) * h % P
  return [x3, y3, mod(z3, P)]
}

const toJac = (pt) => (pt === null ? JAC_INF : [mod(pt[0], P), mod(pt[1], P), 1n])

function fromJac([x, y, z]) {
  if (z === 0n) return null
  const zi = modInverse(z, P)
  const zi2 = mod(zi * zi, P)
  return [mod(x * zi2, P), mod(y * zi2 * zi, P)]
}

/** `k · pt`。标量先对 `N` 取模。 */
export function scalarMul(k, pt) {
  if (pt === null) return null
  let kk = mod(k, N)
  if (kk === 0n) return null
  let result = JAC_INF
  let addend = toJac(pt)
  while (kk > 0n) {
    if (kk & 1n) result = jacAdd(result, addend)
    addend = jacDouble(addend)
    kk >>= 1n
  }
  return fromJac(result)
}

// ---- 序列化 ---------------------------------------------------------------

/** 未压缩编码 `0x04 ‖ x(32) ‖ y(32)`。 */
export function pointBytes(pt) {
  if (pt === null) throw new Error('无穷远点不能序列化')
  const out = new Uint8Array(POINT_BYTES)
  out[0] = 0x04
  let x = pt[0]
  let y = pt[1]
  for (let i = COORD; i > 0; i--) {
    out[i] = Number(x & 0xffn)
    out[COORD + i] = Number(y & 0xffn)
    x >>= 8n
    y >>= 8n
  }
  return out
}

/** 反序列化，**并校验点在曲线上**。 */
export function pointFromBytes(data) {
  if (data.length !== POINT_BYTES) {
    throw new Error(`点必须是 ${POINT_BYTES} 字节（未压缩），收到 ${data.length}`)
  }
  if (data[0] !== 0x04) throw new Error(`不支持的点编码前缀 ${data[0]}`)
  let x = 0n
  let y = 0n
  for (let i = 0; i < COORD; i++) {
    x = (x << 8n) | BigInt(data[1 + i])
    y = (y << 8n) | BigInt(data[1 + COORD + i])
  }
  if (x >= P || y >= P) throw new Error('坐标越出素域')
  const pt = [x, y]
  if (!isOnCurve(pt)) throw new Error('这个点不在 SM2 曲线上')
  return pt
}

// ---- ECDH 与 KDF ----------------------------------------------------------

/** `d · peer` 的 x 坐标，32 字节。 */
export function ecdhShared(d, peer) {
  if (peer === null || !isOnCurve(peer)) throw new Error('对端公钥不在 SM2 曲线上')
  const shared = scalarMul(d, peer)
  if (shared === null) throw new Error('共享点是无穷远点 —— 对端公钥有问题')
  const out = new Uint8Array(COORD)
  let x = shared[0]
  for (let i = COORD - 1; i >= 0; i--) {
    out[i] = Number(x & 0xffn)
    x >>= 8n
  }
  return out
}

/**
 * KDF —— `SM3(shared ‖ counter_be32 ‖ info)`，计数器从 **1** 起。
 *
 * ★ 与 GM/T 0003 的示例写法（counter 在别处）**不同**，
 *   必须以 `core/sm2.py::kdf` 为准，否则派生出来的密钥完全不同。
 */
export function kdf(shared, info, length) {
  const out = new Uint8Array(length)
  const ctr = new Uint8Array(4)
  let counter = 1
  let pos = 0
  while (pos < length) {
    new DataView(ctr.buffer).setUint32(0, counter)
    const h = sm3(concat(shared, ctr, info))
    const n = Math.min(h.length, length - pos)
    out.set(h.subarray(0, n), pos)
    pos += n
    counter += 1
  }
  return out
}
