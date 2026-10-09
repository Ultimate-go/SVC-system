/**
 * 分片异步 KDF 的**等价性**回归（登录等待动画的前提）。
 *
 * ★ 为什么必须钉住：登录改走异步那条路（`pbkdf2Sm3Async` → `unwrapPrivateKeyAsync`），
 *   为的是让"解封私钥"那 2~3 秒不再把主线程占满 —— 但**密钥材料必须与同步版逐位相同**。
 *   差一个字节，用户就再也解不开自己的私钥，而且现象是"口令不对"
 *   （密码学上分不出口令错与密文被改），这是最难查的一类故障。
 *   所以这里给两份实现喂同一组输入，直接比字节。
 *
 * 另一条是**进度回调**：登录页的进度条拿它当真实进度用，
 * 所以它必须单调不减、并且最终收到 1（否则进度条永远差一截）。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { pbkdf2Sm3, pbkdf2Sm3Async } from '../src/utils/crypto/pbkdf2.js'
import {
  KeyWrapFormatError,
  KeyWrapIntegrityError,
  unwrapPrivateKey,
  unwrapPrivateKeyAsync,
} from '../src/utils/crypto/keywrap.js'

const V = JSON.parse(readFileSync(new URL('./crypto-vectors.json', import.meta.url), 'utf8'))

const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')

test('pbkdf2Sm3Async 与 pbkdf2Sm3 逐位相同（含跨块的 64 字节输出）', async () => {
  const salt = new Uint8Array(16).fill(7)
  for (const iters of [1000, 3000]) {
    for (const dkLen of [32, 64]) {
      const sync = pbkdf2Sm3('pass-中文-1', salt, iters, dkLen)
      const async_ = await pbkdf2Sm3Async('pass-中文-1', salt, iters, dkLen)
      assert.equal(async_.length, dkLen)
      assert.equal(hex(async_), hex(sync), `iters=${iters} dkLen=${dkLen} 两种实现必须一致`)
    }
  }
})

test('pbkdf2Sm3Async：进度单调不减，且最后一次正好是 1', async () => {
  const seen = []
  await pbkdf2Sm3Async('pw', new Uint8Array(8), 20000, 32, {
    onProgress: (p) => seen.push(p),
    sliceMs: 0, // 每次检查都让出 ⇒ 进度会被报很多次（测试里要的就是这个）
  })
  assert.ok(seen.length >= 2, `至少要报两次进度，实际 ${seen.length} 次`)
  assert.equal(seen[seen.length - 1], 1, '最后一次必须是 1，否则进度条永远差一截')
  for (let i = 1; i < seen.length; i++) {
    assert.ok(seen[i] >= seen[i - 1], `进度不能倒退：${seen[i - 1]} → ${seen[i]}`)
    assert.ok(seen[i] <= 1, `进度不能超过 1：${seen[i]}`)
  }
})

test('unwrapPrivateKeyAsync：解 Python 封的密文，标量与同步版完全一致', async () => {
  for (const c of V.user_keys) {
    const sync = unwrapPrivateKey(c.password, c.blob)
    const async_ = await unwrapPrivateKeyAsync(c.password, c.blob)
    assert.equal(async_, sync)
    assert.equal(async_, BigInt(c.sk))
  }
})

test('unwrapPrivateKeyAsync：口令错必须报完整性错误（与同步版同一句话）', async () => {
  const c = V.user_keys[0]
  await assert.rejects(() => unwrapPrivateKeyAsync('这不是口令', c.blob), KeyWrapIntegrityError)
})

test('unwrapPrivateKeyAsync：迭代数上限仍然夹住（不因为"异步"就变成免费的 DoS）', async () => {
  const c = V.user_keys[0]
  const bad = { ...c.blob, iters: 3_000_000 }
  await assert.rejects(() => unwrapPrivateKeyAsync(c.password, bad), KeyWrapFormatError)
})
