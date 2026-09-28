/**
 * timing.js 的单元测试 —— Node 自带 node --test，零依赖。
 *
 * 重点钉住两条：
 *  1. buildPerf 的 pct 按「各段之和」算，不按 total_ms 算（total 会略大，差额是埋点没覆盖的零碎）；
 *  2. 这些函数是纯函数，不碰 DOM / Vue，Node 里能直接跑。
 */

import { strict as assert } from 'node:assert'
import { describe, it } from 'node:test'

import {
  buildPerf,
  formatMs,
  humanMs,
  pctOf,
  stageColor,
  timed,
  unwrapAxios,
} from '../src/utils/timing.js'

describe('formatMs / humanMs', () => {
  it('毫秒级保留一位小数', () => {
    assert.equal(formatMs(12.34), '12.3 ms')
  })
  it('秒级换算', () => {
    assert.equal(formatMs(1500), '1.50 s')
  })
  it('humanMs 用整数毫秒', () => {
    assert.equal(humanMs(234), '234 ms')
  })
})

describe('pctOf', () => {
  it('正常比例', () => {
    assert.equal(pctOf(25, 100), 25)
  })
  it('总数为 0 时不除零', () => {
    assert.equal(pctOf(5, 0), 0)
  })
  it('钳在 0~100', () => {
    assert.equal(pctOf(200, 100), 100)
    assert.equal(pctOf(-5, 100), 0)
  })
})

describe('stageColor（阶段名 → 颜色 tone）', () => {
  it('加密 / 封装归 warn', () => {
    assert.equal(stageColor('逐块加密（SM4）'), 'warn')
  })
  it('承诺归 accent', () => {
    assert.equal(stageColor('承诺（commit）'), 'accent')
  })
  it('未知阶段归 muted', () => {
    assert.equal(stageColor('别的'), 'muted')
  })
})

describe('buildPerf（timings → 渲染结构）', () => {
  const timings = {
    total_ms: 120,
    stages: [
      { stage: '切块', ms: 10, count: 1 },
      { stage: '逐块加密（SM4）', ms: 40, count: 3 },
    ],
  }

  it('阶段数一致，且 pct 以各段之和为分母（50 为 100%）', () => {
    const p = buildPerf(timings)
    assert.equal(p.stages.length, 2)
    // 各段之和 = 50；40 占 80%
    const encrypt = p.stages.find((s) => s.stage.includes('加密'))
    assert.equal(encrypt.pct, 80)
    assert.equal(encrypt.count, 3)
  })

  it('total 与 sum 分开给（不摊平差额）', () => {
    const p = buildPerf(timings)
    assert.equal(p.total_ms, 120)
    assert.equal(p.sum_ms, 50)
  })

  it('空 timings 不崩', () => {
    const p = buildPerf(null)
    assert.equal(p.stages.length, 0)
    assert.equal(p.total_ms, 0)
  })
})

describe('unwrapAxios / timed', () => {
  it('unwrapAxios 从响应取 data，裸对象原样返回', () => {
    assert.equal(unwrapAxios({ data: 42 }), 42)
    assert.equal(unwrapAxios(42), 42)
  })
  it('timed 返回 { result, ms }', () => {
    const r = timed(() => 7)
    assert.equal(r.result, 7)
    assert.equal(typeof r.ms, 'number')
  })
})
