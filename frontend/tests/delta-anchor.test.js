/**
 * δ 钉扎（安全审计 S4）—— 浏览器验证的"答案"必须来自**本地**，不能来自服务端。
 *
 * 为什么单开一个文件：这是一条**安全性质**，不是"能不能解开"的功能测试。
 * `addBack` 实现得再正确，如果验证用的目标值 `(U_n, C)` 是服务端那份，
 * 那么一个被控的服务端可以为任意密文造一份自洽的证据 —— 两边都是它算的。
 * 下面的用例就是钉住这一点：**本地 δ 说了算**。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { bytesToHex, hexToBytes } from '../src/utils/crypto/bytes.js'
import { checkModulus, openBlocks } from '../src/utils/crypto/index.js'
import { verifyEvidence } from '../src/utils/crypto/verify.js'
import {
  dropCrsAnchor,
  dropDeltaAnchor,
  getCrsAnchor,
  getDeltaAnchor,
  setCrsAnchor,
  setDeltaAnchor,
} from '../src/utils/anchor.js'

/**
 * localStorage 桩。`anchor.js` **只在函数体内**访问 localStorage，
 * 所以在这里装一个内存实现就够（静态 import 也不受影响）。
 */
class MemStorage {
  constructor() {
    this.m = new Map()
  }
  getItem(k) {
    return this.m.has(String(k)) ? this.m.get(String(k)) : null
  }
  setItem(k, v) {
    this.m.set(String(k), String(v))
  }
  removeItem(k) {
    this.m.delete(String(k))
  }
  clear() {
    this.m.clear()
  }
}
globalThis.localStorage = new MemStorage()

const V = JSON.parse(readFileSync(new URL('./crypto-vectors.json', import.meta.url), 'utf8'))
const { pack, sk, plains_hex } = V.client_pack
const SK = BigInt(sk)
/** 三个块的明文拼起来 —— Python 侧算出来的权威值。 */
const EXPECT_PLAIN = plains_hex.join('')

/** 把一段十六进制的最后一个字节翻一位（模拟"密文被换过"）。 */
function flipLastByte(hex) {
  const b = hexToBytes(hex)
  b[b.length - 1] ^= 1
  return bytesToHex(b)
}

// ---------------------------------------------------------------------------
// openBlocks 的完整通路
// ---------------------------------------------------------------------------

test('openBlocks：完整 /cipher 应答能解开（密文 / 块密钥 / 证据 全部自洽）', () => {
  const opened = openBlocks(pack, SK)
  assert.equal(opened.verify.ok, true, opened.verify.message)
  assert.equal(bytesToHex(opened.plain), EXPECT_PLAIN)
  assert.equal(opened.blocks.length, 3)
  // 没钉过 δ —— 必须如实标出来，不能假装验过了
  assert.equal(opened.deltaVerdict, 'no-anchor')
  assert.equal(opened.delta.offset, 0)
})

test('openBlocks：本地 δ 与服务端一致 → match', () => {
  const opened = openBlocks(pack, SK, {
    trustedDelta: { U: pack.delta.U, C: pack.delta.C },
  })
  assert.equal(opened.deltaVerdict, 'match')
  assert.equal(opened.verify.ok, true)
  assert.equal(opened.remoteVerify, null, '一致时不该再多验一遍')
})

test('★ 回归：offset > 0 的文件（局部块号 ≠ 全局位置）也能开块', () => {
  // 把 `global_index` 整体挪一个偏移，模拟"这不是全局第一份文件"。
  //
  // `proof.I` 是**局部**块号（0,1,2…），所以映射必须按 `block_idx` 建 ——
  // 按 `global_index` 建的话，这个用例会当场报
  // “证据覆盖了第 0 块，但这次没有取它的密文”。
  // （对 offset = 0 的第一份文件两者恰好相等，所以那个错在本地开发时
  //   很容易滑过去 —— 这一条就是为了把它钉死。）
  const shifted = {
    ...pack,
    offset: 100,
    blocks: pack.blocks.map((b, i) => ({ ...b, global_index: 100 + i })),
  }
  const opened = openBlocks(shifted, SK, {
    trustedDelta: { U: pack.delta.U, C: pack.delta.C },
  })
  assert.equal(opened.verify.ok, true, opened.verify.message)
  assert.equal(bytesToHex(opened.plain), EXPECT_PLAIN)
  assert.equal(opened.blocks[0].globalIndex, 100, '全局位置仍然如实带出来（展示要用）')
})

test('openBlocks：账目对不上要当场说清（n 与 δ.n 必须一致）', () => {
  // 把 n 报小一号 ⇒ 与 δ 里记的向量长度对不上（安全审计 N6）。
  // 以前没有这道核对，这个包会一路走到验证那一步、报一句“S_I 被伪造”，
  // 指不到真正的原因 —— 而真正原因（这次交付的账目就是错的）一句话就能说清。
  const bad = { ...pack, n: 2 }
  assert.throws(
    () => openBlocks(bad, SK, { trustedDelta: { U: pack.delta.U, C: pack.delta.C } }),
    /账目不一致/,
  )
})

test('openBlocks：交付块号越界 / 重复，也要当场说清', () => {
  const base = { trustedDelta: { U: pack.delta.U, C: pack.delta.C } }
  const outOfRange = {
    ...pack,
    blocks: [{ ...pack.blocks[0], block_idx: pack.n }, ...pack.blocks.slice(1)],
  }
  assert.throws(() => openBlocks(outOfRange, SK, base), /越界/)
  const dup = {
    ...pack,
    blocks: [pack.blocks[0], { ...pack.blocks[1], block_idx: pack.blocks[0].block_idx }],
  }
  assert.throws(() => openBlocks(dup, SK, base), /重复/)
})

test('verifyEvidence：下标越界要当场说清（与 Python 的 BAD_SHAPE 对齐）', () => {
  // 直接打在验证那一层：`I` 的最后一个下标 >= n ⇒ BAD_SHAPE。
  // （比 openBlocks 那一层晚 —— 那边现在会更早拦住块号越界，所以这条单独测。）
  const r = verifyEvidence({
    N: BigInt(pack.crs.N),
    U_n: BigInt(pack.delta.U),
    C: BigInt(pack.delta.C),
    I: [0, 1],
    values: [1n, 1n],
    S_I: BigInt(pack.proof.S_I),
    Lambda_I: BigInt(pack.proof.Lambda_I),
    primes: [BigInt(pack.primes.values[0]), BigInt(pack.primes.values[1])],
    n: 1,
  })
  assert.equal(r.ok, false)
  assert.equal(r.codeName, 'BAD_SHAPE')
  assert.match(r.message, /越界/)
})

// ---------------------------------------------------------------------------
// 安全审计 S1 的回归网：**群参数 N 也是信任根的一部分**
// ---------------------------------------------------------------------------

test('★ S1：服务端换掉群参数 N —— 必须直接拒绝（本地锚说了算）', () => {
  dropCrsAnchor()
  // ① 第一次接触：本地没有锚 ⇒ 如实标成 no-anchor（本次采信远端）
  const first = openBlocks(pack, SK, { trustedDelta: { U: pack.delta.U, C: pack.delta.C } })
  assert.equal(first.crsVerdict, 'no-anchor')
  assert.equal(first.verify.ok, true)

  // ② 见证一次：把系统参数记下来
  setCrsAnchor({
    N: pack.crs.N,
    l: pack.crs.l,
    prime_bits: pack.crs.prime_bits,
    prime_start: pack.primes.start,
    source: 'test',
  })
  assert.ok(getCrsAnchor(), '系统参数锚应当已经记下')

  const same = openBlocks(pack, SK, { trustedDelta: { U: pack.delta.U, C: pack.delta.C } })
  assert.equal(same.crsVerdict, 'match', '同一个 N 必须判 match')

  // ③ 服务端换一个 N —— 真实攻击里它会挑一个**自己知道阶**的 N'，
  //    以便反解出让 addBack 链收敛到钉住的 (U*, C*) 的 S_I / Λ_I。
  //    这里只要证明“换 N 会被拦住”：形状检查放行、本地锚不放行。
  const evilN = (BigInt(pack.crs.N) + 2n).toString()
  const evil = { ...pack, crs: { ...pack.crs, N: evilN } }
  assert.throws(
    () => openBlocks(evil, SK, { trustedDelta: { U: pack.delta.U, C: pack.delta.C } }),
    /群参数|不一致/,
    '换 N 必须当场拒绝 —— 这就是 S1 的回归网',
  )
  dropCrsAnchor()
})

test('P0-2：明显假造的 N 要被基本形状检查拦下', () => {
  assert.equal(checkModulus(12345678n).ok, false, '偶数不可能是两个奇素数之积')
  assert.equal(checkModulus(3n * 1000000007n).ok, false, '能被小素数整除')
  assert.equal(checkModulus(1n).ok, false, '太小')
  assert.equal(checkModulus(BigInt(pack.crs.N)).ok, true, '正常的 N 应当通过')
})

test('★ S4：服务端把 δ 换成别的值 —— 结论必须由**本地 δ**说了算', () => {
  // 攻击者能控制的就是 pack 里的 δ（它就是验证的"目标值"）。
  const evil = {
    ...pack,
    delta: { ...pack.delta, C: (BigInt(pack.delta.C) + 1n).toString() },
  }
  const opened = openBlocks(evil, SK, {
    // 本地钉的是**真的**那一份
    trustedDelta: { U: pack.delta.U, C: pack.delta.C },
  })

  assert.equal(opened.deltaVerdict, 'mismatch')
  // ★★ 这一条就是 S4 的判据：拿本地 δ 验 —— **通过**（内容与我见证的版本自洽）
  assert.equal(opened.verify.ok, true, '本地 δ 必须仍然通过')
  // 而拿服务端那份（错的）δ 验 —— 不通过
  assert.equal(opened.remoteVerify.ok, false, '服务端那份 δ 必须验不过')
})

test('★ S4：密文被换过 —— 本地 δ 会拒绝（δ 一模一样，但内容变了）', () => {
  const blocks = pack.blocks.map((b, i) =>
    i === 1 ? { ...b, ciphertext_hex: flipLastByte(b.ciphertext_hex) } : b,
  )
  const opened = openBlocks({ ...pack, blocks }, SK, {
    trustedDelta: { U: pack.delta.U, C: pack.delta.C },
  })
  assert.equal(opened.deltaVerdict, 'match', 'δ 没变，所以是 match')
  assert.equal(opened.verify.ok, false, '密文被换 ⇒ 必须拒绝')
  assert.equal(opened.verify.codeName, 'BAD_LAMBDA')
})

test('openBlocks：素数表被塞合数 → 直接拒绝继续（而不是静默算错）', () => {
  const bad = {
    ...pack,
    primes: {
      ...pack.primes,
      values: pack.primes.values.map((v, i) => (i === 1 ? (BigInt(v) + 2n).toString() : v)),
    },
  }
  assert.throws(() => openBlocks(bad, SK), /合数/)
})

// ---------------------------------------------------------------------------
// δ 锚的存取（localStorage）
// ---------------------------------------------------------------------------

test('δ 锚：存 → 读 → 丢', () => {
  localStorage.clear()
  assert.equal(getDeltaAnchor(7), null)
  setDeltaAnchor(7, { U: '111', C: '222', n: 8, fp: 'abc', source: '测试' })
  const a = getDeltaAnchor(7)
  assert.equal(a.U, '111')
  assert.equal(a.C, '222')
  assert.equal(a.n, 8)
  assert.equal(a.source, '测试')
  assert.ok(a.ts > 0)
  dropDeltaAnchor(7)
  assert.equal(getDeltaAnchor(7), null)
})

test('δ 锚：U/C 缺一就不写（钉一个脏锚比没锚更糟）', () => {
  localStorage.clear()
  setDeltaAnchor(9, { U: '', C: '22' })
  assert.equal(getDeltaAnchor(9), null)
  setDeltaAnchor(9, { U: '11', C: '' })
  assert.equal(getDeltaAnchor(9), null)
})

test('δ 锚：按文件隔离（7 号与 8 号互不影响）', () => {
  localStorage.clear()
  setDeltaAnchor(7, { U: '7', C: '7' })
  setDeltaAnchor(8, { U: '8', C: '8' })
  dropDeltaAnchor(7)
  assert.equal(getDeltaAnchor(7), null)
  assert.equal(getDeltaAnchor(8).U, '8')
})
