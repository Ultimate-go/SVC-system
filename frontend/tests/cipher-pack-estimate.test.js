/**
 * 「整份取密文的体积估算」与它的告警阈值（审计 N3）。
 *
 * ★ 为什么值得钉：这个数会**写到一个按钮的悬浮提示上**、并在超过阈值时
 *   弹一次确认（「试解密 / 解密整份」都是整份取，`indices = null`）。
 *   估算偏小会让人以为没多大，偏大又会把人吓跑 —— 所以拿审计实测的三组
 *   数据当锚点：估算必须是**上界**（宁大不小），而且星线上要单调、不溢出。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  estimateCipherPackBytes,
  CIPHER_PACK_WARN_BYTES,
} from '../src/utils/format.js'

const KB = 1024

test('N3：估算不小于审计实测的三组响应体（宁可偏大）', () => {
  // 实测：174300 / 215268 / 297490 字节
  const measured = [
    [64 * KB, 64, 174300],
    [64 * KB, 128, 215268],
    [64 * KB, 256, 297490],
  ]
  for (const [bytes, blocks, actual] of measured) {
    const est = estimateCipherPackBytes(bytes, blocks)
    assert.ok(
      est >= actual,
      `估小了：${blocks} 块 × ${bytes} 字节 → 估算 ${est} < 实测 ${actual}`,
    )
    // 也不能离谱：上界不该超过实测的两倍（否则提示会吓人）。
    assert.ok(est <= actual * 2, `估得太大：${est} vs 实测 ${actual}`)
  }
})

test('N3：随文件大小与块数单调增长，且退化输入不炸', () => {
  assert.ok(estimateCipherPackBytes(2 * KB, 2) > estimateCipherPackBytes(1 * KB, 2))
  assert.ok(estimateCipherPackBytes(1 * KB, 4) > estimateCipherPackBytes(1 * KB, 2))
  // 空/脏输入：返回一个数（不是 NaN / Infinity），界面才敢直接拼到文案里。
  for (const bad of [null, undefined, 0, -1, NaN, 'x']) {
    const v = estimateCipherPackBytes(bad, bad)
    assert.ok(Number.isFinite(v) && v >= 0, `${String(bad)} → ${v}`)
  }
})

test('N3：上传上限那种大文件会超过告警阈值（所以那条确认真的会弹）', () => {
  // 32 MB、自动档 128 块 × 256 KB —— 审计说这档外推 ≈ 65 MB。
  const est = estimateCipherPackBytes(32 * 1024 * KB, 128)
  assert.ok(est > CIPHER_PACK_WARN_BYTES, `估算 ${est} 没超过阈值`)
  assert.ok(est > 60 * 1024 * KB, `量级不对：${est}`)
  // 而我们现在库里那种小文件绝不能弹确认（否则演示每次都多一次点击）。
  assert.ok(estimateCipherPackBytes(12 * KB, 12) < CIPHER_PACK_WARN_BYTES)
  assert.ok(estimateCipherPackBytes(304 * KB, 77) < CIPHER_PACK_WARN_BYTES)
})
