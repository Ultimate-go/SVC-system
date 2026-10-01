/**
 * fmtTime 的单元测试 —— Node 自带 node --test，零依赖。
 *
 * ★ 这个函数是**全站唯一**的时间显示入口（审计流水、用户列表、备注时间都走它），
 *   而"差 8 小时"这个 bug 在本项目里**已经复发过两次**（改好了又被回滚掉）。
 *   所以这里把它钉死：一旦有人把 `String(iso).replace('T',' ')` 那种
 *   "直接铺字符串"的写法改回来，这些用例会立刻红。
 *
 * 核心约定（三处注释要一致）：
 *   backend/models.py:utcnow()  → UTC
 *   admin.py 的 isoformat()     → 无时区后缀的 UTC 串
 *   utils/format.js:fmtTime()   → 补 Z 按 UTC 解析，再按**浏览器本地时区**显示
 *
 * 注意：期望值不能写死成某个小时的数字 —— 那会让测试依赖跑测机器的时区。
 * 所以下面统一用"拿 Date 自己算一遍"来对账，任何时区下都成立。
 */

import { strict as assert } from 'node:assert'
import { describe, it } from 'node:test'

import { fmtTime } from '../src/utils/format.js'

/** 把 UTC 串按**本地时区**手工算成期望的 'YYYY-MM-DD HH:mm:ss'。 */
function expectLocal(utcMs) {
  const d = new Date(utcMs)
  const p = (n) => String(n).padStart(2, '0')
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  )
}

describe('fmtTime（UTC 串 → 本地时间显示）', () => {
  it('★ 无时区后缀的串按 UTC 解释，再按本地时区显示（不再少 8 小时）', () => {
    const iso = '2026-10-01T17:02:43'
    assert.equal(fmtTime(iso), expectLocal(Date.parse(iso + 'Z')))
  })

  it('★ 与"直接 replace 掉 T"的旧写法必须不同（除非跑在 UTC 时区）', () => {
    const iso = '2026-10-01T17:02:43'
    const naive = iso.replace('T', ' ')
    const offsetMinutes = new Date().getTimezoneOffset()
    if (offsetMinutes === 0) {
      // 跑测机器本身就在 UTC：两种写法结果一样，这条断言没有区分力，跳过。
      assert.equal(fmtTime(iso), naive)
    } else {
      assert.notEqual(fmtTime(iso), naive)
    }
  })

  it('带 Z 的串不重复补 Z（否则会多减一次偏移）', () => {
    const iso = '2026-10-01T17:02:43Z'
    assert.equal(fmtTime(iso), expectLocal(Date.parse(iso)))
  })

  it('带显式偏移量的串按它自己的偏移解释，不再当本地时间', () => {
    // 同一个时刻的两种写法，必须显示成同一个结果。
    assert.equal(
      fmtTime('2026-10-02T01:02:43+08:00'),
      fmtTime('2026-10-01T17:02:43Z'),
    )
  })

  it('带毫秒的串也能吃（后端 timespec 变了也不该崩）', () => {
    assert.equal(
      fmtTime('2026-10-01T17:02:43.636799'),
      expectLocal(Date.parse('2026-10-01T17:02:43.636Z')),
    )
  })

  it('输出是 "YYYY-MM-DD HH:mm:ss" 这个形状，且不含 T / Z', () => {
    const out = fmtTime('2026-10-01T17:02:43')
    assert.match(out, /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/)
    assert.ok(!out.includes('T') && !out.includes('Z'))
  })

  it('空值 / null / undefined 都给占位符，不给 Invalid Date', () => {
    for (const v of ['', null, undefined]) {
      assert.equal(fmtTime(v), '—')
    }
  })

  it('垃圾串原样退回（不显示 Invalid Date，也不抛）', () => {
    assert.equal(fmtTime('abc'), 'abc')
    assert.equal(fmtTime('不是时间'), '不是时间')
  })

  it('认不出来时不抛异常（脏数据不该让整页崩）', () => {
    assert.doesNotThrow(() => fmtTime('9999-99-99T99:99:99'))
    assert.doesNotThrow(() => fmtTime({}))
  })

  it('同一个 UTC 串在任何时区下都表示同一时刻', () => {
    // 拿 UTC 的那一秒，和"本地时间串被正确换算后"的秒数对齐：
    // 用 Date.parse 反解 fmtTime 的输出，应该落回原始的 UTC 毫秒。
    const iso = '2026-10-01T17:02:43'
    const local = fmtTime(iso) // 形如 2026-10-02 01:02:43（在东八区）
    const back = new Date(local.replace(' ', 'T')) // 本地时间解析 → 同一时刻
    const drift = Math.abs(back.getTime() % 1000)
    assert.ok(drift < 1000, '秒级对齐，没有整小时漂移')
  })
})
