/**
 * 证据验证 —— 把 `svc/scheme.py::verify` 在**浏览器里**跑一遍。
 *
 * 这一步是"服务器不可信"的核心：服务端交回来的密文与证据，
 * 浏览器**对着自己保存的 δ** 独立验一遍，而不是相信 `/api/query`
 * 里那个 `ok: true`。
 *
 * 算法（论文 §5.2）
 * -----------------
 * 设证据 `π_I = (S_I, Λ_I)` 是"去掉 I 之后那个向量"的摘要。
 * 把 `I` 里每个位置逐个**加回去**（`svc/scheme.py::add_back`）：
 *
 * ```
 * S' = S^{e_i} mod N
 * Λ' = Λ^{e_i} · S^{v_i} mod N        // 用的是**更新前**的 S
 * ```
 *
 * 全部加回之后必须变回 `d(v) = (U_n, C)`：
 *
 * ```
 * S == U_n    且    Λ == C mod N
 * ```
 *
 * 两条等式分别对应不同的失败环节（`BAD_S_I` / `BAD_LAMBDA`），
 * 所以报错能说到"是哪一步没过"，而不是一句"验证失败"。
 *
 * ★ 代价与**文件长度无关**：每次加回的指数只有 `l+1` 位，
 *   `|I|` 次就是 `O(l·|I|)` 次模幂 —— 与后端那条实现同阶。
 */

import { mod, modPow } from './sm2.js'

/** 失败环节（与 `svc/types.py::VerifyCode` 对齐）。 */
export const VerifyCode = {
  OK: 0,
  BAD_SHAPE: 1,
  BAD_S_I: 2,
  BAD_LAMBDA: 3,
}

export const VERIFY_CODE_NAME = {
  0: 'OK',
  1: 'BAD_SHAPE',
  2: 'BAD_S_I',
  3: 'BAD_LAMBDA',
}

/**
 * 把一个位置"加回"摘要。
 *
 * @param {bigint} S 当前 `S`
 * @param {bigint} Lambda 当前 `Λ`
 * @param {bigint} e_i 该位置的素数
 * @param {bigint} v_i 该位置的值（分量）
 * @param {bigint} N 隐藏阶群模数
 * @returns {[bigint, bigint]} 新的 `(S, Λ)`
 */
export function addBack(S, Lambda, e_i, v_i, N) {
  const sNew = modPow(S, e_i, N)
  // ★ `S` 必须是**更新前**的那个（见 `add_back` 的数学推导）
  const lam = mod(modPow(Lambda, e_i, N) * modPow(S, v_i, N), N)
  return [sNew, lam]
}

function fail(code, message) {
  return { ok: false, code, codeName: VERIFY_CODE_NAME[code], message }
}

/**
 * 对着 δ 验证一份证据。
 *
 * @param {object} p
 * @param {bigint} p.N 隐藏阶群模数
 * @param {bigint} p.U_n 摘要里的 `U`（`δ.U`）
 * @param {bigint} p.C 摘要里的 `C`（`δ.C`）
 * @param {number[]} p.I 证据覆盖的下标（升序）
 * @param {bigint[]} p.values 这些下标的值（**由调用方从密文自己算**）
 * @param {bigint} p.S_I 证据的 `S_I`
 * @param {bigint} p.Lambda_I 证据的 `Λ_I`
 * @param {bigint[]} p.primes `I` 各位置对应的素数（**已通过素性检查**）
 * @param {number} [p.n] 这份文件的向量长度 —— 给了就顺手查下标越界
 *   （与 Python ``svc/scheme.py`` 的 ``I[-1] >= n → BAD_SHAPE`` 对齐）
 * @returns {{ok: boolean, code: number, codeName: string, message: string}}
 */
export function verifyEvidence({ N, U_n, C, I, values, S_I, Lambda_I, primes, n = null }) {
  // ---- 形状检查（与 svc.verify 的第一段一致：很便宜，且能指名是哪一份坏了）----
  if (I.length !== values.length) {
    return fail(VerifyCode.BAD_SHAPE, `值的个数（${values.length}）与下标个数（${I.length}）不一致`)
  }
  if (I.length !== primes.length) {
    return fail(VerifyCode.BAD_SHAPE, `素数的个数（${primes.length}）与下标个数（${I.length}）不一致`)
  }
  for (let k = 1; k < I.length; k++) {
    if (I[k] <= I[k - 1]) {
      return fail(VerifyCode.BAD_SHAPE, '下标必须严格递增且不重复')
    }
  }
  // ★ 越界检查（安全审计 P12）：Python 那边有、前端以前没有。
  //   少了它，一个越界的下标会一路走到旁支报错（比如“分量对不上承诺”），
  //   指不到真正原因 —— 而真正原因其实一句就能说清。
  if (n !== null && I.length && I[I.length - 1] >= n) {
    return fail(
      VerifyCode.BAD_SHAPE,
      `下标 ${I[I.length - 1]} 越界：这份文件的向量只有 ${n} 个位置`,
    )
  }

  let S = S_I
  let Lam = Lambda_I
  for (let k = 0; k < I.length; k++) {
    ;[S, Lam] = addBack(S, Lam, primes[k], values[k], N)
  }

  if (S !== mod(U_n, N)) {
    return fail(
      VerifyCode.BAD_S_I,
      'S_I 校验失败：S_I^{e_I} ≠ U_n —— S_I 被伪造，或者下标集合不对',
    )
  }
  if (Lam !== mod(C, N)) {
    return fail(
      VerifyCode.BAD_LAMBDA,
      'Λ_I 校验失败：Λ_I^{e_I} · ∏ S_i^{y_i} ≠ C —— 要么数据被改过，要么 Λ_I 不对',
    )
  }
  return { ok: true, code: VerifyCode.OK, codeName: 'OK', message: '验证通过（浏览器本地）' }
}
