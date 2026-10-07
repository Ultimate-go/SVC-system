/**
 * 素数 —— 校验（默认）与现场重算（严格模式）。
 *
 * 为什么必须校验
 * --------------
 * 方案里每个位置对应一个**互不相同**的素数 `e_i`。如果服务端塞一个**合数**
 * 进来，`mathbase.shamir_trick` 会**静默返回错误结果**（不是崩溃）——
 * 那是最难查的一类 bug（`svc/primegen.py` 的模块说明里专门警告了这一点）。
 *
 * 所以"服务端给的素数表"必须被当成**不可信输入**：
 *
 * * 便宜的检查：位长恰好 `l+1`、是奇数、落在 `[2^l, 2^{l+1})` 内、严格递增；
 * * 贵的检查：**素性**（Miller-Rabin）。只对证据真正用到的那几个下标做 ——
 *   `|I|` 很小，全验也就几十毫秒。
 *
 * 至于"第 i 个素数到底是不是第 i 个"（序列的**完整性**），
 * `primesFrom()` 提供了现场重算那条路（`primegen` 是确定性的：
 * 从 `2^{bits-1}` 起向上数第 i+1 个 bits 位素数）。它慢，所以做成显式选择，
 * 而不是默认路径 —— 默认路径把"用了哪几个素数"验到位就够了。
 *
 * ★ 能力边界（不要夸大 `checkPrimes`）
 * ----------------------------------
 * `checkPrimes` **发现不了"下标与值错位"**：`{0,2,3}` 配 `{e0,e1,e2}` 三项
 * 单调、位长对、都是素数，光看这三项无从判断"第 2 个位置该配哪个素数"。
 * 这不是缺陷，而是输入本身不包含那个信息。
 *
 * 但它**也不构成漏洞**：`U_n` 与 `C` 是由**正确**的素数序列定义的，
 * 换一个素数算完 `addBack` 链，`S` 与 `Λ` 必然对不上它们 —— 验证会拒绝。
 * 所以塞错素数的后果是"验不过"，而不是"验过了"。
 * `checkPrimes` 真正拦的是**合数**（那才会让 shamir_trick 静默算错）。
 */

/** 预筛用的小素数。 */
const SMALL_PRIMES = [
  2n, 3n, 5n, 7n, 11n, 13n, 17n, 19n, 23n, 29n, 31n, 37n, 41n, 43n, 47n,
  53n, 59n, 61n, 67n, 71n, 73n, 79n, 83n, 89n, 97n,
]

/** Miller-Rabin 的固定基。16 轮固定基对合数的误判率 < 4^-16。 */
const MR_BASES = [2n, 3n, 5n, 7n, 11n, 13n, 17n, 19n, 23n, 29n, 31n, 37n, 41n, 43n, 47n, 53n]

function modPow(base, exp, m) {
  let result = 1n
  let b = base % m
  let e = exp
  while (e > 0n) {
    if (e & 1n) result = (result * b) % m
    b = (b * b) % m
    e >>= 1n
  }
  return result
}

/**
 * Miller-Rabin 素性测试。
 *
 * 对 `n ≤ 100` 直接查表；否则先用小素数筛一遍，再跑 16 轮固定基。
 */

/**
 * 校验「服务端给的」群参数 `N`（安全审计 P0-2）。
 *
 * ★ 为什么客户端**必须**自己挡一道：`N` 是验证方程的一部分
 *   （`S == U_n`、`Λ == C (mod N)`）。服务端若能自选一个**已知阶**的 `N'`
 *   （比如 2048 位素数，群阶 `N'-1` 已知），就能用已知阶反解出 `S_I` / `Λ_I`，
 *   使 `addBack` 链收敛到**任意**目标值 —— 那「验证通过」就成了服务端说了算。
 *
 *   本地锚（`utils/anchor.js` 的 `vds_crs_v1`）负责「与**中途**换的 N 对不上」；
 *   这个函数负责「这个 N 本身就不像话」（偶数、太小、位长不符、被小素数整除）。
 *
 * ★ 边界（要写在界面上，不能含糊）：这两道合起来能抓住「中途换 N」与
 *   「明显假造的 N」，**抓不住**「服务端从第一天就给一个形状合法的恶意 N」。
 *   那需要 `N` 有**独立于服务端**的来源（部署配置 / 出带分发 / 客户端自生成）。
 *
 * @param {bigint} N 待检的群模数
 * @param {object} [opts]
 * @param {number|null} [opts.bits] 部署声明的位长 —— 给了就要求精确相等
 * @returns {{ok: boolean, checked: number, ms: number, message: string}}
 */
export function checkModulus(N, { bits = null } = {}) {
  const t0 = Date.now()
  const done = (ok, message) => ({ ok, checked: 0, ms: Date.now() - t0, message })
  if (typeof N !== 'bigint') return done(false, '群参数 N 不是大整数')
  if (N <= 2n) return done(false, `群参数 N = ${N} 太小`)
  // RSA 群模数是两个奇素数之积，必为奇数；偶数意味着它根本不是。
  if ((N & 1n) === 0n) return done(false, '群参数 N 是偶数 —— 不可能是两个奇素数之积')
  let bl = 0n
  for (let x = N; x > 0n; x >>= 1n) bl += 1n
  if (bits !== null && bits !== undefined && Number(bl) !== bits) {
    return done(false, `群参数 N 的位长 ${bl} 与声明不符（应为 ${bits}）`)
  }
  // 能被小素数整除 ⇒ 是造出来的合数，不是两个大素数的积。
  for (const p of SMALL_PRIMES) {
    if (N % p === 0n) {
      return done(false, `群参数 N 能被小素数 ${p} 整除 —— 不是 RSA 模数`)
    }
  }
  return done(true, `位长 ${bl}，通过基本形状检查`)
}

export function isProbablePrime(n) {
  if (n < 2n) return false
  for (const p of SMALL_PRIMES) {
    if (n === p) return true
    if (n % p === 0n) return false
  }

  let d = n - 1n
  let r = 0n
  while ((d & 1n) === 0n) {
    d >>= 1n
    r += 1n
  }

  for (const a of MR_BASES) {
    if (a >= n) continue
    let x = modPow(a, d, n)
    if (x === 1n || x === n - 1n) continue
    let composite = true
    for (let i = 1n; i < r; i++) {
      x = (x * x) % n
      if (x === n - 1n) {
        composite = false
        break
      }
    }
    if (composite) return false
  }
  return true
}

/**
 * 校验一段"服务端给的"素数表。
 *
 * @param {object} p
 * @param {number} p.bits 素数位长（本方案 = `l + 1` = 257）
 * @param {bigint} p.start 序列起点（本该恒为 `2^{bits-1}`）
 * @param {number[]} p.indices 这些素数各自的下标
 * @param {bigint[]} p.values 素数本身，与 `indices` 一一对应
 * @param {bigint} [p.expectStart] 本地锚里记着的起点 —— 给了就要求完全一致
 * @returns {{ok: boolean, checked: number, ms: number, message: string}}
 */
export function checkPrimes({ bits, start, indices, values, expectStart = null }) {
  const t0 = Date.now()
  if (indices.length !== values.length) {
    return { ok: false, checked: 0, ms: 0, message: '下标与素数的个数不一致' }
  }
  if (!Number.isInteger(bits) || bits < 2 || bits > 4096) {
    return { ok: false, checked: 0, ms: 0, message: `素数位长 ${bits} 不合理` }
  }
  // ★★ 基线**必须**是 2^{bits-1}（安全审计 P0-1 第 5 点）。
  //    这一版的素数序列就从那里开始，`lo` 也由它算出来。
  //    以前 `start` 只是“拿来当下界用”、从不核对 —— 于是服务端可以把下界
  //    抬到任意值，摆上一批**真素数**（素性对、递增对、位长对），
  //    客户端全过，但那张表已经不是这份文件该有的表了。
  const base = 1n << BigInt(bits - 1)
  if (start !== undefined && start !== null && start !== base) {
    return {
      ok: false,
      checked: 0,
      ms: Date.now() - t0,
      message: `素数序列起点 ${start} ≠ 2^${bits - 1}（${base}）—— 基线被改过`,
    }
  }
  // ★ 本地锚里记着的起点（首次见证时钉的）：不一致同样是“换了考场”。
  if (expectStart !== null && expectStart !== undefined && start !== expectStart) {
    return {
      ok: false,
      checked: 0,
      ms: Date.now() - t0,
      message: `素数序列起点与本地锚不一致（本次 ${start}，本地记的是 ${expectStart}）`,
    }
  }
  const limit = 1n << BigInt(bits)
  const lo = base

  let prevIdx = -1
  let prevVal = lo - 1n
  for (let k = 0; k < values.length; k++) {
    const e = values[k]
    if (indices[k] <= prevIdx) {
      // 序列是按下标升序给的；不升序说明被拼过
      return { ok: false, checked: k, ms: Date.now() - t0, message: '下标不是严格递增' }
    }
    if (e <= prevVal) {
      return {
        ok: false,
        checked: k,
        ms: Date.now() - t0,
        message: `第 ${indices[k]} 个素数 ${e} 不比前一个小 —— 序列被改过`,
      }
    }
    if (e < lo || e >= limit) {
      return {
        ok: false,
        checked: k,
        ms: Date.now() - t0,
        message: `第 ${indices[k]} 个素数 ${e} 不在 [2^${bits - 1}, 2^${bits}) 内`,
      }
    }
    if ((e & 1n) === 0n) {
      return {
        ok: false,
        checked: k,
        ms: Date.now() - t0,
        message: `第 ${indices[k]} 个"素数"是偶数`,
      }
    }
    if (!isProbablePrime(e)) {
      return {
        ok: false,
        checked: k,
        ms: Date.now() - t0,
        message: `第 ${indices[k]} 个"素数" ${e} 其实是合数 —— 服务端给的素数表不可信`,
      }
    }
    prevIdx = indices[k]
    prevVal = e
  }
  return {
    ok: true,
    checked: values.length,
    ms: Date.now() - t0,
    message: `${values.length} 个素数全部通过素性检查（${Date.now() - t0} ms）`,
  }
}

/**
 * **现场重算**素数序列（严格模式）。
 *
 * `primegen` 是确定性的：从 `start = 2^{bits-1}` 起向上扫描，
 * 第 i 个素数就是"第 i+1 个 bits 位素数"。所以只要扫到 `max(indices)`，
 * 就能**独立**得到同一串，不需要相信服务端。
 *
 * ★ 慢：257 位素数平均要试 ~180 个候选，每个 16 轮 Miller-Rabin。
 *   下标很大时（比如 8191）会非常久 —— 所以它是显式开关，不是默认。
 *
 * @param {bigint} start 起点（`2^{bits-1}`）
 * @param {number} bits 位长
 * @param {number} count 要几个
 * @param {(found: number, need: number) => void} [onProgress]
 */
export function primesFrom(start, bits, count, onProgress) {
  const limit = 1n << BigInt(bits)
  const out = []
  let cand = start % 2n === 0n ? start | 1n : start
  while (out.length < count) {
    if (cand >= limit) {
      throw new Error(`${bits} 位素数已经用尽：只找到 ${out.length} 个，但需要 ${count} 个`)
    }
    if (isProbablePrime(cand)) {
      out.push(cand)
      if (onProgress) onProgress(out.length, count)
    }
    cand += 2n
  }
  return out
}
