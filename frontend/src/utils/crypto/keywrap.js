/**
 * 密钥解封 —— 把 Python 侧 `core/keywrap.py` 的两层封装在**浏览器里**打开。
 *
 * 这是"私钥留在客户端"的落地：后端只把**密文**（用户的私钥密文、每块的
 * 块密钥密文）交给浏览器，口令与私钥**都不出浏览器**。
 *
 * 两层各解什么
 * ------------
 * 1. `unwrapPrivateKey(password, blob)` —— 用口令解出**用户私钥**（SM2 标量）。
 *    这一层的存在意义是：数据库被拿走也解不开，因为口令不落库。
 * 2. `unwrapKey(sk, ct)` —— 用私钥解出**某一块的块密钥**（16 字节 SM4 密钥）。
 *    密文是拿**所有者公钥**封的，所以别人的私钥在这一步会因 tag 不匹配而失败
 *    —— "解密受限"那道门卡在这里，而不是卡在某个 if 判断上。
 *
 * 格式标识（KIND）必须先比对：老格式的密文喂进来应当立刻得到
 * "这不是本格式"，而不是一个看不懂的代数错误。
 */

import {
  bigIntToBytes,
  bytesToBigInt,
  bytesToHex,
  bytesEq,
  concat,
  hexToBytes,
  utf8,
} from './bytes.js'
import { pbkdf2Sm3, pbkdf2Sm3Async } from './pbkdf2.js'
import { ecdhShared, kdf, pointFromBytes } from './sm2.js'
import { sm4Ctr } from './sm4.js'

/** 块密钥密文的格式标识（与 `core/keywrap.py::KIND_BLOCK` 一致）。 */
export const KIND_BLOCK = 'sm2-ecies-v1'
/** 用户私钥密文的格式标识（与 `core/keywrap.py::KIND_USER_KEY` 一致）。 */
export const KIND_USER_KEY = 'userkey-pbkdf2sm3-v1'

const INFO_ENC = utf8('vds/ecies/v1/enc')
const INFO_TAG = utf8('vds/ecies/v1/tag')
const INFO_UK_ENC = utf8('vds/userkey/v1/enc')
const INFO_UK_TAG = utf8('vds/userkey/v1/tag')

const SM4_KEY_BYTES = 16
const SM4_IV_BYTES = 16
const TAG_BYTES = 32
const PRIVATE_KEY_BYTES = 32
/** SM2 阶（用于夹住解出来的标量范围）。 */
const SM2_N = 0xfffffffeffffffffffffffffffffffff7203df6b21c6052b53bbf40939d54123n

/** 解封失败（密文被改、或用的不是对应的密钥）。 */
export class KeyWrapIntegrityError extends Error {}
/** 密文形状/格式不对。 */
export class KeyWrapFormatError extends Error {}

function expectObj(ct, what, kind) {
  if (ct === null || typeof ct !== 'object') {
    throw new KeyWrapFormatError(`${what}必须是对象`)
  }
  if (ct.kind !== kind) {
    throw new KeyWrapFormatError(
      `${what}的格式是 ${JSON.stringify(ct.kind)}，本实现只认 ${kind}（旧格式的密文会被拦在这里）`,
    )
  }
  return ct
}

function field(obj, name, size) {
  const raw = obj[name]
  if (typeof raw !== 'string') throw new KeyWrapFormatError(`字段 ${name} 应当是十六进制字符串`)
  const out = hexToBytes(raw)
  if (size !== undefined && out.length !== size) {
    throw new KeyWrapFormatError(`字段 ${name} 应当是 ${size} 字节，收到 ${out.length}`)
  }
  return out
}

/**
 * 用所有者的私钥解出一把**块密钥**。
 *
 * @param {bigint} sk 所有者私钥标量
 * @param {object} ct 块密钥密文（`{kind, R, iv, body, tag}`）
 * @returns {Uint8Array} 16 字节块密钥
 * @throws {KeyWrapIntegrityError} tag 对不上（密文被改，或这不是你的私钥）
 */
export function unwrapKey(sk, ct) {
  expectObj(ct, '块密钥密文', KIND_BLOCK)
  const rBytes = field(ct, 'R', 65)
  const iv = field(ct, 'iv', SM4_IV_BYTES)
  const body = field(ct, 'body')
  const tag = field(ct, 'tag', TAG_BYTES)

  let rPoint
  try {
    rPoint = pointFromBytes(rBytes)
  } catch (e) {
    throw new KeyWrapFormatError(`密文里的临时公钥非法：${e.message}`)
  }

  const z = ecdhShared(sk, rPoint)
  const expectTag = kdf(z, concat(INFO_TAG, rBytes, iv, body), TAG_BYTES)
  if (!bytesEq(expectTag, tag)) {
    // ★ 这是**密码学那道门**：不是"查表发现你没权限"，而是**算不出来**。
    throw new KeyWrapIntegrityError(
      '块密钥密文的认证标签对不上 —— 密文被改过，或者这不是你的私钥',
    )
  }
  const sm4Key = kdf(z, INFO_ENC, SM4_KEY_BYTES)
  return sm4Ctr(body, sm4Key, iv)
}

/**
 * 随机字节（浏览器 / Node 通用）。
 *
 * 先试 WebCrypto，没有就退回 `Math.random` —— 后者**不是密码学安全**的，
 * 只用于“跑得起来”的降级（真实浏览器与 Node 18+ 都有 `getRandomValues`）。
 */
function randomBytes(n) {
  const out = new Uint8Array(n)
  const g = globalThis.crypto
  if (g && typeof g.getRandomValues === 'function') {
    g.getRandomValues(out)
    return out
  }
  // ★★ 宁可**抛错**，也不静默退回 `Math.random`（安全审计 N2）。
  //
  //   这里生成的是**盐与 IV** —— 弱随机会让"只有自己能解密"这句话打折，
  //   而调用方**无从察觉**（拿到的是一个形状完全正常的密文）。
  //   真实浏览器与 Node 18+ 都有 `getRandomValues`，所以这条路只在
  //   "环境根本不支持 WebCrypto"时才会走到；那时正确的做法是明说
  //   "这个环境跑不了"，而不是悄悄降级。
  throw new Error(
    '这个环境没有可用的密码学随机源（crypto.getRandomValues）—— ' +
      '拒绝用不安全的随机数生成密钥材料。请换用现代浏览器（或 Node 18+）。',
  )
}

/**
 * 用**新口令**把私钥重新封装 —— 改口令时在**浏览器**里做（安全审计 I8）。
 *
 * ★ 为什么需要它：默认模型下服务端**不持有私钥**，于是
 * “后端拿旧私钥重封”那条改密路径必然 403（`session_key_of` 恒为 None）。
 * 用户为了改密码就只能先以 `server_key=true` 登录 —— 那等于
 * 习惯性地把私钥交回服务端，整套“服务端不掌握私钥”被绕过。
 *
 * 这里把“重封”放在**浏览器**：口令与私钥都不出本机，只把**密文**交给服务端。
 * 服务端那边会验证两件事（见 `backend/manager.py::rekey_with_blob`）：
 * ① 新密文能用新口令解开；② 里面的私钥**还是这个人原来那把**
 * —— 否则“用别人的私钥替换”就能冒充。
 *
 * 形状与 `core/keywrap.py::wrap_private_key` **逐字节一致**：
 * `KEK = PBKDF2-HMAC-SM3(pw, salt, iters)`；`body = SM4-CTR(sk‖32字节大端)`；
 * `tag = KDF(KEK, "vds/userkey/v1/tag" ‖ salt ‖ iv ‖ body)`。
 *
 * @param {string} password 新口令
 * @param {bigint} sk 私钥标量
 * @param {number} [iterations] 迭代数（与后端 PBKDF2_ITERATIONS 对齐）
 * @param {Uint8Array} [salt] 盐（不传就现场抽 16 字节；测试会传固定值）
 * @returns {object} 可直接交给后端的私钥密文（dict）
 */
export function wrapPrivateKey(password, sk, { iterations = 200000, salt = null } = {}) {
  if (sk < 1n || sk >= SM2_N) throw new KeyWrapFormatError('私钥标量必须落在 [1, N-1]')
  if (!Number.isInteger(iterations) || iterations < 1000) {
    throw new KeyWrapFormatError(`迭代数太低（${iterations}），至少 1000`)
  }
  const s = salt || randomBytes(16)
  if (s.length < 8) throw new KeyWrapFormatError('盐至少 8 字节')

  const kek = pbkdf2Sm3(password, s, iterations, 32)
  const sm4Key = kdf(kek, INFO_UK_ENC, SM4_KEY_BYTES)
  const iv = randomBytes(SM4_IV_BYTES)
  const body = sm4Ctr(bigIntToBytes(sk, PRIVATE_KEY_BYTES), sm4Key, iv)
  const tag = kdf(kek, concat(INFO_UK_TAG, s, iv, body), TAG_BYTES)

  return {
    kind: KIND_USER_KEY,
    salt: bytesToHex(s),
    iters: iterations,
    iv: bytesToHex(iv),
    body: bytesToHex(body),
    tag: bytesToHex(tag),
  }
}

/**
 * 用口令解出**用户私钥**标量。
 *
 * @param {string} password 登录口令
 * @param {object} blob 私钥密文（`{kind, salt, iters, iv, body, tag}`）
 * @returns {bigint} 私钥标量
 * @throws {KeyWrapIntegrityError} 口令不对，或者密文被改过
 *   （★ 这两件事在密码学上分不开，所以给同一句话 —— 不给攻击者"口令对不对"的 oracle）
 */
export function unwrapPrivateKey(password, blob) {
  expectObj(blob, '用户私钥密文', KIND_USER_KEY)
  const salt = field(blob, 'salt')
  const iv = field(blob, 'iv', SM4_IV_BYTES)
  const body = field(blob, 'body')
  const tag = field(blob, 'tag', TAG_BYTES)

  const iters = blob.iters
  if (!Number.isInteger(iters) || iters < 1000) {
    throw new KeyWrapFormatError(`字段 iters 不合法：${JSON.stringify(iters)}`)
  }
  if (iters > 2_000_000) {
    // ★ 与后端的 PBKDF2_ITERATIONS_MAX 对齐：不夹住就是一条免费的 DoS
    //   （一份被改过的密文能让浏览器卡死很久）。
    throw new KeyWrapFormatError(`迭代数太高（${iters}）—— 这份密钥密文可能被改过`)
  }

  const kek = pbkdf2Sm3(password, salt, iters, 32)
  const expectTag = kdf(kek, concat(INFO_UK_TAG, salt, iv, body), TAG_BYTES)
  if (!bytesEq(expectTag, tag)) {
    throw new KeyWrapIntegrityError('口令不对，或者私钥密文被改过')
  }
  const sm4Key = kdf(kek, INFO_UK_ENC, SM4_KEY_BYTES)
  const raw = sm4Ctr(body, sm4Key, iv)
  const sk = bytesToBigInt(raw)
  if (sk < 1n || sk >= SM2_N) throw new KeyWrapFormatError('解出来的私钥标量越界，密文可能损坏')
  return sk
}

/**
 * `unwrapPrivateKey` 的**分片异步**版本：结果完全一样，只是中途会让出主线程。
 *
 * ★ 存在的理由：解私钥这一下要跑 20 万轮 PBKDF2-HMAC-SM3（纯 JS，2~3 秒）。
 *   同步版会把主线程占满 —— 登录页那句“正在解封…”的动画会当场冻住。
 *   这里把同一套计算切成 10ms 一片，界面照常刷新（计算量与结果都没变）。
 *
 *   `onProgress` 收到的 0→1 就是**派生 KEK 的完成度** —— 解封耗时几乎全在这
 *   一步（剩下的 SM4 解密与标签比对只有毫秒级），所以它可以当作整段的进度。
 *
 * @param {string} password 登录口令
 * @param {object} blob 私钥密文（`{kind, salt, iters, iv, body, tag}`）
 * @param {{onProgress?: (fraction: number) => void}} [options]
 * @returns {Promise<bigint>} 私钥标量
 * @throws {KeyWrapIntegrityError} 口令不对，或者密文被改过（与同步版同一句话）
 */
export async function unwrapPrivateKeyAsync(password, blob, options = {}) {
  expectObj(blob, '用户私钥密文', KIND_USER_KEY)
  const salt = field(blob, 'salt')
  const iv = field(blob, 'iv', SM4_IV_BYTES)
  const body = field(blob, 'body')
  const tag = field(blob, 'tag', TAG_BYTES)

  const iters = blob.iters
  if (!Number.isInteger(iters) || iters < 1000) {
    throw new KeyWrapFormatError(`字段 iters 不合法：${JSON.stringify(iters)}`)
  }
  if (iters > 2_000_000) {
    // ★ 与后端的 PBKDF2_ITERATIONS_MAX 对齐：不夹住就是一条免费的 DoS。
    throw new KeyWrapFormatError(`迭代数太高（${iters}）—— 这份密钥密文可能被改过`)
  }

  const kek = await pbkdf2Sm3Async(password, salt, iters, 32, options)
  const expectTag = kdf(kek, concat(INFO_UK_TAG, salt, iv, body), TAG_BYTES)
  if (!bytesEq(expectTag, tag)) {
    throw new KeyWrapIntegrityError('口令不对，或者私钥密文被改过')
  }
  const sm4Key = kdf(kek, INFO_UK_ENC, SM4_KEY_BYTES)
  const raw = sm4Ctr(body, sm4Key, iv)
  const sk = bytesToBigInt(raw)
  if (sk < 1n || sk >= SM2_N) throw new KeyWrapFormatError('解出来的私钥标量越界，密文可能损坏')
  return sk
}
