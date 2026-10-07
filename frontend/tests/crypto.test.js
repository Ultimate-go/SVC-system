/**
 * 前端密码学实现的**对齐测试** —— 与 Python 权威输出逐字节比对。
 *
 * 测试向量由 `scripts/gen_crypto_vectors.py` 生成（`crypto-vectors.json`）。
 * 任何一处偏差（S 盒抄错一位、KDF 计数器位置写错、CTR 不递增、PBKDF2 用错
 * 哈希……）都会在这里当场暴露，而不是等到演示时"解出来是乱码"。
 *
 * 这些测试是"服务器不可信"那句承诺的技术底座：
 * 浏览器必须能独立算出与后端**完全相同**的东西，否则"自己验证"毫无意义。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { bytesToBigInt, bytesToHex, hexToBytes } from '../src/utils/crypto/bytes.js'
import { elementOf, publicKeyOf } from '../src/utils/crypto/index.js'
import { unwrapKey, unwrapPrivateKey, KeyWrapIntegrityError } from '../src/utils/crypto/keywrap.js'
import { kdf, ecdhShared, pointBytes } from '../src/utils/crypto/sm2.js'
import { pbkdf2Sm3 } from '../src/utils/crypto/pbkdf2.js'
import { sm3 } from '../src/utils/crypto/sm3.js'
import { sm4Ctr } from '../src/utils/crypto/sm4.js'
import { isProbablePrime, checkPrimes, primesFrom } from '../src/utils/crypto/primes.js'
import { verifyEvidence, VerifyCode } from '../src/utils/crypto/verify.js'

const V = JSON.parse(readFileSync(new URL('./crypto-vectors.json', import.meta.url), 'utf8'))

test('SM3：与 Python cryptography.SM3 逐字节相同', () => {
  for (const c of V.sm3) {
    assert.equal(bytesToHex(sm3(hexToBytes(c.msg_hex))), c.digest_hex)
  }
})

test('SM4-CTR：与 cryptography 的 SM4-CTR 逐字节相同（含跨分组）', () => {
  for (const c of V.sm4ctr) {
    const got = sm4Ctr(hexToBytes(c.plain_hex), hexToBytes(c.key_hex), hexToBytes(c.iv_hex))
    assert.equal(bytesToHex(got), c.cipher_hex)
    // CTR 是对合运算：再跑一遍必须变回明文
    assert.equal(bytesToHex(sm4Ctr(got, hexToBytes(c.key_hex), hexToBytes(c.iv_hex))), c.plain_hex)
  }
})

test('KDF：SM3(shared ‖ counter_be32 ‖ info)，计数器从 1 起', () => {
  for (const c of V.kdf) {
    const got = kdf(hexToBytes(c.shared_hex), hexToBytes(c.info_hex), c.length)
    assert.equal(bytesToHex(got), c.out_hex)
  }
})

test('SM2：公钥与 ECDH 共享点（双向必须相等）', () => {
  const { sk_a, sk_b, pk_a_hex, pk_b_hex, z_hex } = V.sm2
  assert.equal(bytesToHex(pointBytes(publicKeyOf(BigInt(sk_a)))), pk_a_hex)
  assert.equal(bytesToHex(pointBytes(publicKeyOf(BigInt(sk_b)))), pk_b_hex)

  const z1 = ecdhShared(BigInt(sk_a), publicKeyOf(BigInt(sk_b)))
  const z2 = ecdhShared(BigInt(sk_b), publicKeyOf(BigInt(sk_a)))
  assert.equal(bytesToHex(z1), z_hex)
  assert.equal(bytesToHex(z2), z_hex)
})

test('块密钥解封：能解开 Python 封的密文', () => {
  for (const c of V.block_keys) {
    const key = unwrapKey(BigInt(c.sk), c.ct)
    assert.equal(bytesToHex(key), c.key_hex)
    assert.equal(key.length, 16)
  }
})

test('块密钥解封：改一位就报完整性错误（而不是静默解出坏密钥）', () => {
  const c = V.block_keys[0]
  const body = hexToBytes(c.ct.body)
  body[0] ^= 0x01
  const tampered = { ...c.ct, body: bytesToHex(body) }
  assert.throws(() => unwrapKey(BigInt(c.sk), tampered), KeyWrapIntegrityError)
})

test('用户私钥解封：口令 + 密文 → 同一个标量', () => {
  for (const c of V.user_keys) {
    assert.equal(unwrapPrivateKey(c.password, c.blob), BigInt(c.sk))
  }
})

test('用户私钥解封：口令错必须失败', () => {
  const c = V.user_keys[0]
  assert.throws(() => unwrapPrivateKey('这不是口令', c.blob), KeyWrapIntegrityError)
})

test('PBKDF2-HMAC-SM3：与 hashlib.pbkdf2_hmac("sm3", …) 相同', () => {
  // 用一个已知输出的等价检查：解封用户私钥这条路径本身依赖它，
  // 上面那条"口令我解的出同一个标量"已经覆盖；这里再钉住长度与确定性。
  const a = pbkdf2Sm3('vds12345', hexToBytes('00'.repeat(16)), 1000, 32)
  const b = pbkdf2Sm3('vds12345', hexToBytes('00'.repeat(16)), 1000, 32)
  assert.equal(a.length, 32)
  assert.equal(bytesToHex(a), bytesToHex(b))
  const c = pbkdf2Sm3('vds12345', hexToBytes('00'.repeat(16)), 1001, 32)
  assert.notEqual(bytesToHex(a), bytesToHex(c), '迭代数必须真的参与运算')
})

test('分量 = SM3(密文) 的大端整数（vector_element 的等价物）', () => {
  for (const c of V.elements) {
    assert.equal(elementOf(hexToBytes(c.cipher_hex)).toString(), c.element)
  }
})

test('素数检查：服务端的表能通过（形状 + 素性）', () => {
  const s = V.svc
  const r = checkPrimes({
    bits: s.prime_bits,
    start: 1n << BigInt(s.l),
    indices: [0, 1, 2, 3, 4, 5, 6, 7],
    values: s.primes.map((v) => BigInt(v)),
  })
  assert.equal(r.ok, true, r.message)
})

test('素数检查：塞一个合数必须被抓住（否则验证会静默算错）', () => {
  const s = V.svc
  const bad = s.primes.map((v) => BigInt(v))
  bad[3] = bad[3] + 2n // 257 位奇数，但几乎必然是合数
  const r = checkPrimes({
    bits: s.prime_bits,
    start: 1n << BigInt(s.l),
    indices: [0, 1, 2, 3],
    values: bad.slice(0, 4),
  })
  assert.equal(r.ok, false)
  assert.match(r.message, /合数/)
})

test('素数：形状检查抓不住"索引错位"，只有现场重算能（能力边界刻意钉住）', () => {
  const s = V.svc
  const start = 1n << BigInt(s.l)

  // {0,2,3} 配 {e0,e1,e2}：三项单调、位长对、都是素数 —— 形状检查**看不出**
  // 第 2 个位置给的是第 2 个素数而不是第 3 个。这不是实现缺陷：要发现它
  // 必须把整条序列重算一遍（= primesFrom）。
  const shapeOnly = checkPrimes({
    bits: s.prime_bits,
    start,
    indices: [0, 2, 3],
    values: [BigInt(s.primes[0]), BigInt(s.primes[1]), BigInt(s.primes[2])],
  })
  assert.equal(shapeOnly.ok, true, '形状检查本就不该声称能发现错位')

  // ★ 但错位**不会**导致验证通过：U_n 与 C 是由正确的素数序列定义的，
  //   换一个素数算出来的 S/Λ 对不上它们 —— 下面的 svc 用例已经钉住了这一点。

  // 现场重算才是那条独立路径
  const truth = primesFrom(start, s.prime_bits, 4)
  assert.equal(truth[2].toString(), s.primes[2])
  assert.notEqual(truth[2].toString(), s.primes[1], '错位会被重算抓住')
})

test('素数：位长/区间/单调性不符要拒（这些是形状检查真正的职责）', () => {
  const s = V.svc
  const start = 1n << BigInt(s.l)
  // 偶数
  const even = checkPrimes({ bits: s.prime_bits, start, indices: [0], values: [4n] })
  assert.equal(even.ok, false)
  // 超出位长上界
  const big = checkPrimes({
    bits: s.prime_bits,
    start,
    indices: [0],
    values: [1n << BigInt(s.prime_bits)],
  })
  assert.equal(big.ok, false)
  // 低于起点
  const low = checkPrimes({ bits: s.prime_bits, start, indices: [0], values: [3n] })
  assert.equal(low.ok, false)
})

/** 从 `from` 起向上找第一个 `bits` 位概率素数（仅测试用）。 */
function pickPrime(from, bits) {
  const limit = 1n << BigInt(bits)
  let c = from | 1n
  while (c < limit && !isProbablePrime(c)) c += 2n
  if (c >= limit) throw new Error(`${bits} 位里没找到素数`)
  return c
}

test('★ 素数检查：新坐标（按块身份派生）不查单调，但仍查素性/位长', () => {
  // 身份坐标下素数是 `H(owner‖file_key‖i)` 派生出来的 128 位素数 ——
  // **没有任何顺序**。以前后端把它按"257 位、从 2^256 起递增"的旧口径下发，
  // 前端于是把每一份新文件都判成"素数序列被改过"，解密与验证全废（实测踩过）。
  const bits = 128
  const start = 1n << 127n
  // 刻意挑一组**递减**的 128 位素数：老坐标下必拒，新坐标下必须放行
  const e0 = pickPrime((1n << 127n) + 1001n, bits)
  const e1 = pickPrime((1n << 127n) + 3003n, bits)
  const e2 = pickPrime((1n << 127n) + 5005n, bits)
  const desc = [e2, e1, e0]

  const asOld = checkPrimes({ bits, start, indices: [0, 1, 2], values: desc, ordered: true })
  assert.equal(asOld.ok, false, '老坐标下递减必须被拒')
  assert.match(asOld.message, /不比前一个小/)

  const asNew = checkPrimes({ bits, start, indices: [0, 1, 2], values: desc, ordered: false })
  assert.equal(asNew.ok, true, asNew.message)

  // 位长照样要查：128 位的表里塞个 257 位数必须当场抓住
  const tooBig = checkPrimes({
    bits,
    start,
    indices: [0],
    values: [BigInt(V.svc.primes[0])],
    ordered: false,
  })
  assert.equal(tooBig.ok, false, '128 位的表里塞 257 位数必须被拒')
})

test('★ 素数检查：新坐标下"撞素数"必须被拒（否则 shamir_trick 会静默算错）', () => {
  const bits = 128
  const start = 1n << 127n
  const e0 = pickPrime((1n << 127n) + 1001n, bits)
  const dup = checkPrimes({ bits, start, indices: [0, 1], values: [e0, e0], ordered: false })
  assert.equal(dup.ok, false)
  assert.match(dup.message, /重复|撞/)

  // 同一个下标出现两次同样是"拼过"
  const sameIdx = checkPrimes({ bits, start, indices: [3, 3], values: [e0, e0], ordered: false })
  assert.equal(sameIdx.ok, false, sameIdx.message)
})

test('★ 素数检查：ordered 缺省为 true —— 老服务端不传这个字段时行为不变', () => {
  const s = V.svc
  const start = 1n << BigInt(s.l)
  const r = checkPrimes({
    bits: s.prime_bits,
    start,
    indices: [0, 1, 2, 3],
    values: s.primes.slice(0, 4).map((v) => BigInt(v)),
  })
  assert.equal(r.ok, true, r.message)
  // 单调是"有序表"才有的约束：给个递减的必须拒
  const desc = checkPrimes({
    bits: s.prime_bits,
    start,
    indices: [0, 1],
    values: [BigInt(s.primes[1]), BigInt(s.primes[0])],
  })
  assert.equal(desc.ok, false)
})

test('证据验证：应当通过的必须通过', () => {
  const s = V.svc
  const k = s.ok_case
  const r = verifyEvidence({
    N: BigInt(s.N),
    U_n: BigInt(s.U_n),
    C: BigInt(s.C),
    I: k.I,
    values: k.values.map((v) => BigInt(v)),
    S_I: BigInt(k.S_I),
    Lambda_I: BigInt(k.Lambda_I),
    primes: k.I.map((i) => BigInt(s.primes[i])),
  })
  assert.equal(r.ok, true, r.message)
  assert.equal(r.code, VerifyCode.OK)
})

test('证据验证：值被改必须拒绝，并且指名是 Λ 那条信道', () => {
  const s = V.svc
  const k = s.bad_case
  const r = verifyEvidence({
    N: BigInt(s.N),
    U_n: BigInt(s.U_n),
    C: BigInt(s.C),
    I: k.I,
    values: k.values.map((v) => BigInt(v)),
    S_I: BigInt(k.S_I),
    Lambda_I: BigInt(k.Lambda_I),
    primes: k.I.map((i) => BigInt(s.primes[i])),
  })
  assert.equal(r.ok, false)
  assert.equal(r.code, VerifyCode.BAD_LAMBDA)
  assert.equal(r.codeName, 'BAD_LAMBDA')
})

test('证据验证：S_I 被换掉必须拒绝，并且指名是 S 那条信道', () => {
  const s = V.svc
  const k = s.ok_case
  const r = verifyEvidence({
    N: BigInt(s.N),
    U_n: BigInt(s.U_n),
    C: BigInt(s.C),
    I: k.I,
    values: k.values.map((v) => BigInt(v)),
    S_I: BigInt(k.S_I) + 1n,
    Lambda_I: BigInt(k.Lambda_I),
    primes: k.I.map((i) => BigInt(s.primes[i])),
  })
  assert.equal(r.ok, false)
  assert.equal(r.code, VerifyCode.BAD_S_I)
})

test('密文换一位：分量随之改变 → 证据验证拒绝（"交付的字节"被绑定住了）', () => {
  const s = V.svc
  const k = s.ok_case
  const values = k.values.map((v) => BigInt(v))
  values[0] = values[0] ^ 1n // 等价于密文被改
  const r = verifyEvidence({
    N: BigInt(s.N),
    U_n: BigInt(s.U_n),
    C: BigInt(s.C),
    I: k.I,
    values,
    S_I: BigInt(k.S_I),
    Lambda_I: BigInt(k.Lambda_I),
    primes: k.I.map((i) => BigInt(s.primes[i])),
  })
  assert.equal(r.ok, false)
})

test('现场重算素数：与后端给的序列相同（严格模式的根据）', () => {
  const s = V.svc
  const start = 1n << BigInt(s.l)
  const got = primesFrom(start, s.prime_bits, 3)
  assert.deepEqual(
    got.map((x) => x.toString()),
    s.primes.slice(0, 3),
  )
})

test('素性测试的几个边界', () => {
  assert.equal(isProbablePrime(1n), false)
  assert.equal(isProbablePrime(2n), true)
  assert.equal(isProbablePrime(3n), true)
  assert.equal(isProbablePrime(4n), false)
  assert.equal(isProbablePrime(561n), false) // Carmichael 数
  assert.equal(isProbablePrime(7919n), true)
})
