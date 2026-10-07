/**
 * 前端密码学 —— "浏览器自己解密、自己验证"那条路的门面。
 *
 * 这条路的由来
 * ------------
 * 在此之前，解密与验证都在**服务端**做（`/api/files/{id}/decrypt` 用会话私钥
 * 解开、`/api/query` 返回它算好的 `ok`）。那种结构下"服务器不可信"这句话
 * 根本无从谈起 —— 服务端想给你什么就给什么，浏览器没有任何独立判据。
 *
 * 这里把两件事搬到浏览器：
 *
 * 1. **解密**（`unwrapPrivateKey` → `unwrapKey` → `sm4Ctr`）：
 *    口令与私钥都不出浏览器，后端只提供密文；
 * 2. **验证**（`elementOf` → `verifyEvidence`）：
 *    分量由浏览器从密文**自己算**，证据对着**自己保存的 δ**验。
 *
 * 于是服务端唯一的作恶方式变成"交一份过不了验证的应答"，
 * 而那会被当场拒绝。它无法在不被发现的前提下替换内容 —— 除非它能同时
 * 改掉浏览器本地保存的 δ（那已经不是"服务端"了）。
 *
 * 诚实边界
 * --------
 * 默认路径下**素数表来自服务端**（做形状检查 + 素性检查）；
 * `primesFrom()` 提供了现场重算那条完全独立的路径，但它慢，是显式选择。
 * 参见 `primes.js` 顶部的说明。
 */

import { crsFingerprint, getCrsAnchor } from '../anchor.js'
import { bytesToBigInt, concat, hexToBytes } from './bytes.js'
import { unwrapKey } from './keywrap.js'
import { checkModulus, checkPrimes } from './primes.js'
import { G, scalarMul } from './sm2.js'
import { sm3 } from './sm3.js'
import { sm4Ctr } from './sm4.js'
import { verifyEvidence } from './verify.js'

export * from './bytes.js'
export { sm3, sm3Hex } from './sm3.js'
export { sm4Ctr } from './sm4.js'
export * from './sm2.js'
export { hmacSm3, pbkdf2Sm3 } from './pbkdf2.js'
export {
  KIND_BLOCK,
  KIND_USER_KEY,
  KeyWrapFormatError,
  KeyWrapIntegrityError,
  unwrapKey,
  unwrapPrivateKey,
} from './keywrap.js'
export { VERIFY_CODE_NAME, VerifyCode, addBack, verifyEvidence } from './verify.js'
export { checkModulus, checkPrimes, isProbablePrime, primesFrom } from './primes.js'

/** 公钥 = `d·G`（自检/调试用）。 */
export function publicKeyOf(d) {
  return scalarMul(d, G)
}

/** 向量分量 = `SM3(密文)` 的大端整数 —— `core/crypto.py::vector_element` 的等价物。 */
export function elementOf(cipher) {
  return bytesToBigInt(sm3(cipher))
}

/**
 * **客户端开块**：从 `/api/files/{id}/cipher` 的应答里把内容解出来，
 * 并对着 `pack.delta` 里的 δ 独立验证一遍。
 *
 * 步骤（每一步都在浏览器里，服务端不参与）：
 *
 * 1. 校验服务端给的素数（形状 + 素性）—— 塞合数会让验证静默算错；
 * 2. 逐块：`v_i = SM3(密文)` → 解封块密钥（SM2 ECDH）→ SM4 解密；
 * 3. 用**自己算的** `v_i` 跑 `addBack` 链，判 `S == U_n` 且 `Λ == C`。
 *
 * ★★ δ 从哪来（安全审计 S4）
 * --------------------------
 * 第 3 步的两个“目标值” `U_n` / `C` **不能**默认取自服务端返回的那一份 ——
 * 那等于让攻击者自带答案：它可以自选 `(l, N, g, 素数表, δ, S_I, Λ_I)`，
 * 为**任意密文**重算出一份自洽的证据，`addBack` 必然通过。
 *
 * 所以本函数接受 ``opts.trustedDelta``：**本地钉住的 δ**（见 `utils/anchor.js`）。
 * 传了它就用它验（“交付的密文与我见证过的那个版本自洽”）；
 * 没传（第一次见证）才退回用远端 δ，并在返回值里如实标出 ``deltaVerdict``：
 *
 * * ``'no-anchor'`` —— 本地没钉过，本次用的是**服务端给的** δ（首次见证）；
 * * ``'match'``    —— 本地与远端一致；
 * * ``'mismatch'`` —— **不一致**：已用**本地 δ** 定性，并额外用远端 δ 再验一次，
 *   结果放在 ``remoteVerify`` 里。两者分开报，因为“不一致”有**两种**原因：
 *   *(a)* 文件真的被你自己/别人改过（合法，δ 本来就该变）；
 *   *(b)* 服务端在换 δ 以洗白内容（恶意）。
 *   区分办法就是“本地 δ 验得过吗”：验得过 ⇒ 内容与**我见证的那一版**一致；
 *   验不过 ⇒ 内容不是那一版（界面要报警，**不能**拿远端 δ 救场）。
 *
 * @param {object} pack `/api/files/{id}/cipher` 的响应
 * @param {bigint} sk 用户私钥（`unwrapPrivateKey` 解出来的）
 * @param {object} [opts]
 * @param {{U: string, C: string}} [opts.trustedDelta] 本地钉住的 δ 本体
 * @returns {object} `{blocks, verify, remoteVerify, deltaVerdict, primeCheck, delta, plain, ms}`
 * @throws {Error} 素数检查未过、密文长度对不上、或块密钥解不开（不是你的私钥）
 */
export function openBlocks(pack, sk, opts = {}) {
  const t0 = Date.now()

  // ---- ⓪ 信任根：群参数 N 与素数基线（安全审计 S1 / P0-1 / P0-2）----
  //
  // ★★ 这是整条链的**地基**，也是最容易被忽略的一环。
  //
  //   验证等式 `S == U_n`、`Λ == C (mod N)` 里的 `N` **本身就是方程的一部分**。
  //   以前 `N` 每次都从 `pack.crs.N` 现取，而本地锚里只钉了 `(U, C)` ——
  //   于是服务端可以自选一个**已知阶**的 `N'`（例如 2048 位素数，群阶 `N'-1` 已知），
  //   用已知阶反解出 `S_I` / `Λ_I`，让 `addBack` 链**恰好**收敛到钉住的 `(U*, C*)`：
  //   “验证通过”与“δ 一致”同时成立，而密文是它随便给的。
  //
  //   现在：本地锚里有系统参数就用**本地的**；与远端对不上就**直接拒绝**
  //   （不像 δ 那样“再用远端验一遍” —— `N` 变了就是换了考场，用新 N 重验毫无意义）。
  //   本地还没有锚（第一次接触）才采信远端，并把 `crsVerdict` 如实标成 `'no-anchor'`。
  const remoteN = BigInt(pack.crs.N)
  const remoteBits = pack.crs.prime_bits ?? null

  // 先看这个 N 本身像不像话（偶数 / 太小 / 位长不符 / 被小素数整除）
  const modCheck = checkModulus(remoteN, { bits: pack.crs.N_bits ?? null })
  if (!modCheck.ok) {
    throw new Error(`群参数 N 未通过检查，拒绝继续：${modCheck.message}`)
  }

  const crsAnchor = opts.crsAnchor === undefined ? getCrsAnchor() : opts.crsAnchor
  let crsVerdict = 'no-anchor'
  let N = remoteN
  let primeBits = remoteBits
  if (crsAnchor && crsAnchor.N) {
    const sameN = BigInt(crsAnchor.N) === remoteN
    const sameBits =
      crsAnchor.prime_bits == null || remoteBits == null
        ? true
        : Number(crsAnchor.prime_bits) === Number(remoteBits)
    if (sameN && sameBits) {
      crsVerdict = 'match'
      N = BigInt(crsAnchor.N)
      if (crsAnchor.prime_bits != null) primeBits = Number(crsAnchor.prime_bits)
    } else {
      crsVerdict = 'mismatch'
      throw new Error(
        '系统群参数与本地记录的不一致 —— 拒绝继续验证。\n' +
          `  本地锚： N ${crsFingerprint(crsAnchor.N)}，素数位长 ${crsAnchor.prime_bits}\n` +
          `  本次收到：N ${crsFingerprint(remoteN)}，素数位长 ${remoteBits}\n` +
          '  N 变了就是「换了考场」：拿新的 N 重新验证没有意义 —— 服务端可以自选一个\n' +
          '  已知阶的 N，反解出能让任何密文“通过”的证据。\n' +
          '  若这确实是换了一次部署（群参数被重建），请到「个人中心」清掉本地群参数锚，\n' +
          '  再重新见证一次。',
      )
    }
  }

  // ---- ① 素数：当成不可信输入 ----
  const primeCheck = checkPrimes({
    bits: primeBits,
    start: BigInt(pack.primes.start),
    indices: pack.primes.indices,
    values: pack.primes.values.map((v) => BigInt(v)),
    expectStart:
      crsAnchor && crsAnchor.prime_start != null ? BigInt(crsAnchor.prime_start) : null,
  })
  if (!primeCheck.ok) {
    throw new Error(`素数检查未通过，拒绝继续：${primeCheck.message}`)
  }

  // ---- ①′ 账目核对：向量长度两处必须一致；块号唯一且不越界（安全审计 N6）----
  //
  // ★ 以前只逐块比“解密后的长度与 plain_len”，**没有**核对“这次的账目对不对”。
  //   漏交 / 重复交 / 交错的块号都会一路走到验证那一步，报出来的却是
  //   “S_I 被伪造”之类，指不到真正的原因。
  const deltaN = pack.delta?.n ?? null
  if (pack.n != null && deltaN != null && Number(pack.n) !== Number(deltaN)) {
    throw new Error(
      `账目不一致：应答说向量长 ${pack.n}，δ 说 ${deltaN} —— 其中一个是假的，拒绝继续`,
    )
  }
  const seenIdx = new Set()
  for (const b of pack.blocks) {
    const bi = b.block_idx
    if (!Number.isInteger(bi) || bi < 0 || (pack.n != null && bi >= pack.n)) {
      throw new Error(`交付的块号 ${bi} 越界（这份文件的向量只有 ${pack.n} 个位置）`)
    }
    if (seenIdx.has(bi)) throw new Error(`交付的块号 ${bi} 重复出现 —— 应答被拼过`)
    seenIdx.add(bi)
  }

  // ---- ② 逐块：分量 → 块密钥 → 明文 ----
  const blocks = pack.blocks.map((b) => {
    const ct = hexToBytes(b.ciphertext_hex)
    const element = elementOf(ct)
    const key = unwrapKey(sk, b.key_ct) // 解不开会抛 KeyWrapIntegrityError
    const plain = sm4Ctr(ct, key, hexToBytes(b.iv_hex))
    return {
      blockIdx: b.block_idx,
      globalIndex: b.global_index,
      plain,
      element,
      /** 服务端声称的分量 —— 只用于对照，**不是**判定依据。 */
      elementServer: b.element == null ? null : BigInt(b.element),
      elementAgrees: b.element != null && element === BigInt(b.element),
      lenOk: plain.length === b.plain_len,
      holder: b.holder,
    }
  })

  const badLen = blocks.filter((b) => !b.lenOk)
  if (badLen.length) {
    throw new Error(
      `解密后的长度与账目不符（第 ${badLen.map((b) => b.blockIdx).join('、')} 块）—— ` +
        '密文或密钥被换过',
    )
  }

  // ---- ③ 用自己的分量跑证据验证（★ 目标值优先用**本地钉住的 δ**）----
  //
  // ★★ 这里必须用**文件内局部块号**（`block_idx`）做映射，不能用全局位置
  //    （`global_index`）：`proof.I` 是**局部**号（每份文件有自己的素数视图，
  //    从 0 开始），而 `global_index` 是它在全局向量里的位置。
  //
  //    对 offset = 0 的第一份文件两者恰好相等，所以这个错**只在后面的文件上
  //    暴露**（报“证据覆盖了位置 0，但这次没有取它的密文”）。
  //    实测确认：`test_64KB`（offset=28）的 `proof.I = [0,1]`，
  //    而 `blocks[].global_index = [28,29]`、`block_idx = [0,1]`。
  const valmap = new Map(blocks.map((b) => [b.blockIdx, b.element]))
  const I = pack.proof.I
  const values = I.map((g) => {
    const v = valmap.get(g)
    if (v === undefined) throw new Error(`证据覆盖了第 ${g} 块，但这次没有取它的密文`)
    return v
  })
  const primes = pack.primes.values.map((v) => BigInt(v))
  const S_I = BigInt(pack.proof.S_I)
  const Lambda_I = BigInt(pack.proof.Lambda_I)

  const remote = {
    U: String(pack.delta.U),
    C: String(pack.delta.C),
    n: pack.delta.n,
    offset: pack.delta.offset,
    fp: pack.delta.fp,
  }
  const local = opts.trustedDelta || null
  const deltaVerdict = !local
    ? 'no-anchor'
    : String(local.U) === remote.U && String(local.C) === remote.C
      ? 'match'
      : 'mismatch'

  // ★★ 这一句就是 S4 的修复本体：**本地有 δ 就用本地的**。
  //    没有它，验证的"答案"就是服务端自己给的，等于没验。
  const useU = local ? BigInt(local.U) : BigInt(remote.U)
  const useC = local ? BigInt(local.C) : BigInt(remote.C)

  const verify = verifyEvidence({
    N,
    U_n: useU,
    C: useC,
    I,
    values,
    S_I,
    Lambda_I,
    primes,
    // 顺手把向量长度带上：下标越界就能当场说清，而不是绕到旁支报错
    n: pack.n ?? null,
  })

  // 本地与远端不一致时，再用远端 δ 验一遍 —— 这只是为了把"两种原因"分开，
  // **不是**拿它救场：定性结论始终以本地 δ 为准。
  let remoteVerify = null
  if (deltaVerdict === 'mismatch') {
    remoteVerify = verifyEvidence({
      N,
      U_n: BigInt(remote.U),
      C: BigInt(remote.C),
      I,
      values,
      S_I,
      Lambda_I,
      primes,
      n: pack.n ?? null,
    })
  }

  return {
    blocks,
    verify,
    /** 只在不一致时非 null：拿**远端** δ 再验一次的结果（用于区分"被改过"与"被换 δ"）。 */
    remoteVerify,
    /** `'no-anchor'` / `'match'` / `'mismatch'` —— 界面必须如实显示它。 */
    deltaVerdict,
    primeCheck,
    /** `'no-anchor'` / `'match'` —— 群参数（信任根）的核对结论；`'mismatch'` 会直接抛错。 */
    crsVerdict,
    /** 本次**实际生效**的群参数（本地锚优先）。 */
    crs: { N: String(N), prime_bits: primeBits, remote_fp: crsFingerprint(remoteN) },
    delta: remote,
    /** 本地钉住的那一份 δ（没钉过就是 null）。 */
    localDelta: local,
    plain: concat(...blocks.map((b) => b.plain)),
    ms: Date.now() - t0,
  }
}
