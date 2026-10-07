/**
 * 后端 `detail` 的**可读化**测试（审计 F1 的后半条）。
 *
 * ★ 为什么值得单开一份：FastAPI 的参数校验错误（422）里 `detail` 是**数组**，
 *   而 Element Plus 的 message 只接受字符串 ⇒ 直接塞进去弹出来的是一团乱码，
 *   用户拿不到任何可读原因。最容易撞上的场景正是"一次带超过 8192 个下标"
 *   （块数没有上限，「入池」那条路修之前就会走到这里）。
 *
 *   这里钉的是"**至少能看见原因**"：字段名 + 原话。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { readableDetail } from '../src/api/client.js'

test('字符串：原样返回', () => {
  assert.equal(readableDetail('这台机器没跟上'), '这台机器没跟上')
})

test('★ 数组（pydantic 422）：拼成一句人话，且带上字段名', () => {
  const detail = [
    {
      type: 'too_long',
      loc: ['body', 'indices'],
      msg: 'List should have at most 8192 items after validation, not 8300',
      input: new Array(8300).fill(1),
      ctx: { max_length: 8192 },
    },
  ]
  const out = readableDetail(detail)
  assert.equal(
    out,
    'indices：List should have at most 8192 items after validation, not 8300',
  )
  assert.ok(!out.includes('[object'), '不能出现 [object Object]')
  assert.ok(!out.includes('{'), '不能把整个对象 stringify 出去')
})

test('数组里多条：用中文分号连起来', () => {
  const out = readableDetail([
    { loc: ['body', 'indices'], msg: '太长' },
    { loc: ['body', 'allow_partial'], msg: '类型不对' },
  ])
  assert.equal(out, 'indices：太长；allow_partial：类型不对')
})

test('数组里混着字符串 / null：跳过空的，不留空档', () => {
  assert.equal(readableDetail(['第一句', null, '第二句']), '第一句；第二句')
})

test('对象：取 msg / message / detail 里最先有的那个', () => {
  assert.equal(readableDetail({ msg: '一句话' }), '一句话')
  assert.equal(readableDetail({ detail: '嵌套的一句' }), '嵌套的一句')
  assert.equal(
    readableDetail({ message: 'message 字段' }),
    'message 字段',
  )
})

test('空值：返回空串（调用方 `detail || 兜底文案` 才不会写出 "null"）', () => {
  assert.equal(readableDetail(null), '')
  assert.equal(readableDetail(undefined), '')
  assert.equal(readableDetail(''), '')
})

test('怪东西：不抛异常，给出可读的样子', () => {
  assert.equal(readableDetail(42), '42')
  assert.equal(readableDetail({ a: 1 }), '{"a":1}')
})
