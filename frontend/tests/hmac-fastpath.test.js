/**
 * HMAC-SM3「密钥备好」快路径的**等价性**回归。
 *
 * ★ 为什么单独钉一遍：PBKDF2 一轮 20 万次 HMAC 用的都是同一个口令，于是
 *   `pbkdf2.js` 里把 ipad/opad 那两块的中间状态先算一次、之后一直复用
 *   （每次 HMAC 从 4 次压缩降到 2 次、且不再分配临时数组，实测快 7 倍）。
 *   这条路**踩过坑**：分组是"32 位大端字"，而我一开始图省事借了
 *   `Uint8Array` 视图往 `Uint32Array` 里写字节 —— x86 上那是小端映射，
 *   字节序当场反了（块里第一个字变成 0x80636261），算出来的 KEK 全错，
 *   现象是"口令永远不对"。
 *
 *   所以这里放一份**直白的 RFC 2104 参考实现**（只用 `sm3()` 与普通数组拼接，
 *   与快路径不共享任何代码），把 0–80 字节的消息全扫一遍比字节 ——
 *   它抓的就是"打包字节序"这类错。
 *
 * 另一条钉的是整个 PBKDF2 的第一步：`T_1 = HMAC(P, S ‖ 00000001)`。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { hmacSm3, pbkdf2Sm3 } from '../src/utils/crypto/pbkdf2.js'
import { sm3 } from '../src/utils/crypto/sm3.js'

const BLOCK = 64

const cat = (a, b) => {
  const out = new Uint8Array(a.length + b.length)
  out.set(a)
  out.set(b, a.length)
  return out
}

const hex = (bytes) => [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('')

/** RFC 2104 的直白实现 —— 本文件的参考（刻意不复用快路径的任何代码）。 */
function refHmac(key, msg) {
  let k = key
  if (k.length > BLOCK) k = sm3(k)
  const padded = new Uint8Array(BLOCK)
  padded.set(k)
  const ipad = new Uint8Array(BLOCK)
  const opad = new Uint8Array(BLOCK)
  for (let i = 0; i < BLOCK; i++) {
    ipad[i] = padded[i] ^ 0x36
    opad[i] = padded[i] ^ 0x5c
  }
  return sm3(cat(opad, sm3(cat(ipad, msg))))
}

/** 造一条可复现的消息（长度 n）。 */
function message(n, seed = 1) {
  const m = new Uint8Array(n)
  for (let i = 0; i < n; i++) m[i] = (i * 37 + seed * 11) & 0xff
  return m
}

test('hmacSm3：消息长度 0–80 全部与 RFC 2104 参考实现逐位相同', () => {
  const keys = [
    new Uint8Array(0), // 空密钥
    message(8, 3), // 短于一块
    new Uint8Array(BLOCK).fill(0xa5), // 正好一块
    message(BLOCK + 1, 5), // 比一块长 ⇒ 密钥要先摘要
  ]
  for (const key of keys) {
    for (let n = 0; n <= 80; n++) {
      const msg = message(n, 7)
      assert.equal(
        hex(hmacSm3(key, msg)),
        hex(refHmac(key, msg)),
        `key=${key.length}B msg=${n}B 两条路必须一致`,
      )
    }
  }
})

test('hmacSm3：同一输入稳定（不因复用草稿区而串味）', () => {
  const key = message(9, 2)
  const first = hex(hmacSm3(key, message(32, 4)))
  // 中间穿插若干不同长度的调用，再算一次同样的输入
  for (const n of [0, 20, 55, 56, 100]) hmacSm3(message(n, 6), message(n, 8))
  assert.equal(hex(hmacSm3(key, message(32, 4))), first)
})

test('pbkdf2Sm3：iterations=1 时就是 T_1 = HMAC(P, S ‖ 00000001)', () => {
  const salt = message(16, 9)
  const pw = 'vds12345'
  const msg = cat(salt, new Uint8Array([0, 0, 0, 1]))
  const want = refHmac(new TextEncoder().encode(pw), msg)
  assert.equal(hex(pbkdf2Sm3(pw, salt, 1, 32)), hex(want))
})

test('pbkdf2Sm3：迭代数变化会真的改变结果（不是把次数当摆设）', () => {
  const salt = message(16, 9)
  const a = hex(pbkdf2Sm3('vds12345', salt, 2, 32))
  const b = hex(pbkdf2Sm3('vds12345', salt, 3, 32))
  assert.notEqual(a, b)
})
