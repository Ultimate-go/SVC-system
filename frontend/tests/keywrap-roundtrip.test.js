/**
 * 私钥重封的**跨实现**回归（安全审计 I8）。
 *
 * 改口令这条路如果只在浏览器里自洽、而服务端解不开（或反过来），
 * 后果是**用户再也登不进去**。所以两个方向都要钉住：
 *
 * * 「Python 封 → JS 解」：`crypto-vectors.json` 的 `user_keys` 用例覆盖；
 * * 「JS 封 → Python 解」：本文件把 JS 的产物**写到盘上**
 *   （`logs/js-wrapped.json`），再由 `scripts/check_js_wrap.py` 用
 *   `core.keywrap.unwrap_private_key` 去解 —— 见验收文档 10.x。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs'
import { dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  KeyWrapFormatError,
  KeyWrapIntegrityError,
  unwrapPrivateKey,
  wrapPrivateKey,
} from '../src/utils/crypto/keywrap.js'

const V = JSON.parse(readFileSync(new URL('./crypto-vectors.json', import.meta.url), 'utf8'))

/** JS 产物落到盘上的位置（`logs/` 是 gitignored 的）。 */
const OUT = fileURLToPath(new URL('../../logs/js-wrapped.json', import.meta.url))

test('wrapPrivateKey ↔ unwrapPrivateKey 自洽（新口令重封后解得回来）', () => {
  const sk = BigInt(V.sm2.sk_a)
  const blob = wrapPrivateKey('new-pass-1', sk, { iterations: 1000, salt: new Uint8Array(16) })
  assert.equal(unwrapPrivateKey('new-pass-1', blob), sk)
  // 口令错必须失败（与 Python 侧同一句话，不给 oracle）
  assert.throws(() => unwrapPrivateKey('wrong-pass', blob), KeyWrapIntegrityError)
})

test('wrapPrivateKey：密文形状与 core/keywrap.py::wrap_private_key 一致', () => {
  const sk = BigInt(V.sm2.sk_a)
  const blob = wrapPrivateKey('pw', sk, { iterations: 1000, salt: new Uint8Array(16) })
  assert.equal(blob.kind, 'userkey-pbkdf2sm3-v1')
  assert.equal(blob.iters, 1000)
  assert.equal(blob.salt.length, 32, '盐 16 字节 → 32 个十六进制字符')
  assert.equal(blob.iv.length, 32)
  assert.equal(blob.body.length, 64, '私钥是定长 32 字节')
  assert.equal(blob.tag.length, 64, '标签 32 字节')
  // 键集合必须完全一致（少一个字段 Python 侧就会抛 KeyWrapFormatError）
  assert.deepEqual(
    Object.keys(blob).sort(),
    ['body', 'iters', 'iv', 'kind', 'salt', 'tag'],
  )
})

test('wrapPrivateKey：私钥越界 / 迭代数太低要拒（与后端同一套边界）', () => {
  assert.throws(() => wrapPrivateKey('pw', 0n, { iterations: 1000 }), KeyWrapFormatError)
  assert.throws(() => wrapPrivateKey('pw', 1n, { iterations: 10 }), KeyWrapFormatError)
})

test('★ I8：把 JS 封的密文落盘，交给 Python 侧去解（跨实现互通）', () => {
  const sk = BigInt(V.sm2.sk_a)
  const password = 'cross-impl-pass'
  const blob = wrapPrivateKey(password, sk, { iterations: 1000, salt: new Uint8Array(16) })
  mkdirSync(dirname(OUT), { recursive: true })
  writeFileSync(
    OUT,
    JSON.stringify({ password, sk: sk.toString(), blob }, null, 1),
    'utf-8',
  )
  // 本端先自证一遍（Python 那一半见 scripts/check_js_wrap.py）
  assert.equal(unwrapPrivateKey(password, blob), sk)
})
