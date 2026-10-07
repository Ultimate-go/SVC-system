/**
 * 自动切块阶梯的**对齐测试**（前端那份）。
 *
 * ★ 为什么要有它：`src/utils/split.js` 是
 *   `backend/config.py::auto_segment_bytes` 的副本（上传前那句"本次切法"
 *   得当场显示）。副本最大的风险是**悄悄走偏** —— 所以这里钉的档位表
 *   与 `scripts/verify_segment_ladder.py`（后端那份）**逐行相同**：
 *   一边改了档位、另一边没跟上，就有一侧的测试变红。
 *
 * 下面这些数不是"实现算出来什么"，而是**需求**：
 * "十几 KB 的文件不能整块一块" —— 那是用户直接反馈过的问题。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  autoSegmentBytes,
  ladderOptsFrom,
  SEGMENT_LADDER,
  SEGMENT_TARGET_BLOCKS,
} from '../src/utils/split.js'

const KB = 1024

//: ★ 与 `scripts/verify_segment_ladder.py` 第 5 节那张表**逐行相同**。
const PIN = [
  [1, 1024],
  [13, 1024],
  [128, 1024],
  [129, 4096],
  [512, 4096],
  [1024, 16384],
  [2048, 16384],
  [8 * 1024, 65536],
  [32 * 1024, 262144],
]

test('自动切块：小文件落到 1 KB（"还不如原来 1 KB"那条反馈）', () => {
  for (const kb of [1, 8, 13, 64, 128]) {
    assert.equal(autoSegmentBytes(kb * KB), 1024, `${kb} KB 不该被切成大块`)
  }
})

test('自动切块：钉死的档位表（与后端同表）', () => {
  for (const [kb, want] of PIN) {
    assert.equal(autoSegmentBytes(kb * KB), want, `${kb} KB → 期望 ${want}`)
  }
})

test('自动切块：块数不失控（普通大小都在目标以内，兜底档除外）', () => {
  const maxBytes = 1 << 20
  for (let kb = 1; kb <= 4096; kb += 7) {
    const n = kb * KB
    const seg = autoSegmentBytes(n)
    const blocks = Math.ceil(n / seg)
    if (seg === maxBytes) continue // 兜底档：已经没有更大的块可用了
    assert.ok(blocks <= SEGMENT_TARGET_BLOCKS, `${kb} KB 切了 ${blocks} 块`)
  }
})

test('自动切块：单调 —— 文件更大时块大小不会变小', () => {
  let prev = 0
  for (let kb = 1; kb <= 4096; kb += 3) {
    const seg = autoSegmentBytes(kb * KB)
    assert.ok(seg >= prev, `${kb} KB 的块大小 ${seg} 小于上一档 ${prev}`)
    prev = seg
  }
})

test('自动切块：只在阶梯上取值（或允许的最大块）', () => {
  for (let kb = 1; kb <= 8192; kb += 11) {
    const seg = autoSegmentBytes(kb * KB)
    assert.ok(
      SEGMENT_LADDER.includes(seg) || seg === 1 << 20,
      `${kb} KB 算出了野生档位 ${seg}`,
    )
  }
})

test('自动切块：边界与兜底', () => {
  assert.equal(autoSegmentBytes(0), 1024, '空文件不炸')
  assert.equal(autoSegmentBytes(1), 1024)
  assert.equal(autoSegmentBytes(64 * 1024 * KB), 1 << 20, '超大文件兜底到最大块')
  assert.equal(autoSegmentBytes(13 * KB, { minBytes: 4096 }), 4096, 'lo 抬高时最小档跟着抬')
  assert.equal(
    autoSegmentBytes(5 * KB, { ladder: [], maxBytes: 1 << 20 }),
    1 << 20,
    '阶梯为空时兜底到 max，不抛异常',
  )
})

test('ladderOptsFrom：从 /api/status 取参，缺失时退回本地默认', () => {
  const st = {
    segment_ladder: [2048, 8192],
    segment_target_blocks: 64,
    segment_bytes_min: 64,
    segment_bytes_max: 4096,
  }
  const o = ladderOptsFrom(st)
  assert.deepEqual(o.ladder, [2048, 8192])
  assert.equal(o.targetBlocks, 64)
  assert.equal(o.maxBytes, 4096)
  // 8192 超出 maxBytes ⇒ 不可用；2048 下 13 KB = 7 块 ≤ 64 ⇒ 取 2048
  assert.equal(autoSegmentBytes(13 * KB, o), 2048)
  // 什么都没有时用本地默认
  assert.deepEqual(ladderOptsFrom(null).ladder, SEGMENT_LADDER)
  assert.equal(ladderOptsFrom(undefined).targetBlocks, SEGMENT_TARGET_BLOCKS)
})
