/**
 * 输入校验的**报错文案**与**规模闸门**（`src/utils/validate.js`）。
 *
 * ★ 为什么值得单独钉：
 *
 * 1. 同一个 `parseIndexRange` 被两条路用：主路径填的是「第几块」，
 *    高级路径填的是全局编号。以前两条路共用一套写死「下标」的报错 ——
 *    用户在界面上明明填的是块号，报错却跟他说「下标」（用户反馈的
 *    「界面上不该出现全局下标」是同一条线）。所以有了 `noun` 参数，
 *    这里钉住两边各自说自己的话。
 *
 * 2. 规模闸门必须**在展开之前**判（安全审计 I6）：先 for 展开的话，
 *    填一个 `1-999999999` 就能把页面卡死。下面那条断言如果哪天有人
 *    把顺序改回去，会直接超时/OOM 而不是悄悄变慢 —— 这正是我们要的。
 *
 * 3. ★ 坐标口径（2026-10-12 定）：**人读的 1 起、机器读的 0 起** ——
 *    输入解析接受 1 起并 −1 换回内部值，所以这里断言的是
 *    “填 1-4 拿到 [0,1,2,3]” 与 “填 0 必须报错”。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'

import { parseBlockRange, parseIndexRange } from '../src/utils/validate.js'

/**
 * 取「本该抛出的那个错误的 message」。
 *
 * ★ 注意：Node 的 ``assert.throws`` **不返回**错误对象（返回 undefined），
 *   所以要 msg 得自己接一下 —— 一开始写错了，测试直接红。
 */
function thrownMessage(fn) {
  try {
    fn()
  } catch (e) {
    return e.message
  }
  throw new Error('本该抛错，却没有')
}

test('主路径（第几块）：报错说「块」，不说「下标」', () => {
  const msg = thrownMessage(() => parseBlockRange('1-99999999', 100))
  assert.match(msg, /个块/)
  assert.doesNotMatch(msg, /下标/)
})

test('高级路径（全局编号）：报错照旧说「下标」', () => {
  assert.match(thrownMessage(() => parseIndexRange('1-99999999')), /个下标/)
})

test('语法错：说清该填什么（且两条路同一套说法）', () => {
  assert.match(thrownMessage(() => parseIndexRange('12x')), /格式不合法/)
  assert.match(thrownMessage(() => parseIndexRange('12x')), /从 1 开始/)
  assert.match(thrownMessage(() => parseBlockRange('12x', 10)), /格式不合法/)
})

test('★ 人读 1 起：填 0 必须报错，填 1 对应内部 0', () => {
  assert.match(thrownMessage(() => parseIndexRange('0')), /从 1 开始/)
  assert.match(thrownMessage(() => parseBlockRange('0-3', 100)), /从 1 开始/)
  assert.deepEqual(parseIndexRange('1'), [0])
  assert.deepEqual(parseIndexRange('1-3'), [0, 1, 2])
})

test('规模闸门在展开之前判（填一个天文数字不会把页面拖死）', () => {
  // 真去展开的话：1e9 个元素 —— 这条测试会直接跑不完。
  assert.match(thrownMessage(() => parseIndexRange('1-999999999', 10000)), /单次上限 10000 个/)
  // ★ 注意措辞：在 `parseBlockRange` 这条路上，「块」出现在前半句
  //   （「将展开为 1000000000 个块」），后半句只是「单次上限 1142 个」。
  const msg = thrownMessage(() => parseBlockRange('1-999999999', 1142))
  assert.match(msg, /将展开为 999999999 个块/)
  assert.match(msg, /单次上限 1142 个/)
  assert.doesNotMatch(msg, /下标/)
})

test('块号越界：说清这份文件只有几块', () => {
  const msg = thrownMessage(() => parseBlockRange('1, 6', 3))
  assert.match(msg, /该文件共 3 块/)
  assert.match(msg, /第 1 至 3 块/)
  assert.match(msg, /超出部分：6/)
})

test('N2：块数超过 10000 的文件，也只能一次填 10000 块', () => {
  // 块数**没有上限**（64 字节一块的话 1 MB 就是 16384 块）：只拿块数当上限
  // 会放行一个 >10000 的区间，然后由 /api/query/files 回 400。
  // 所以两边取小 —— 这一步必须在**前端**拦住。
  assert.throws(() => parseBlockRange('1-16384', 16384), /单次上限 10000 个/)
  // 10000 以内不受影响：16384 块的文件里填 1-101 照样合法。
  assert.equal(parseBlockRange('1-101', 16384).length, 101)
  // 刚好 10000 块允许，多一块就拦。
  assert.equal(parseBlockRange('1-10000', 16384).length, 10000)
  assert.throws(() => parseBlockRange('1-10001', 16384), /单次上限 10000 个/)
})

test('正常解析不受影响（区间 / 逗号 / 去重排序；入参 1 起、出参 0 起）', () => {
  assert.deepEqual(parseIndexRange('1-4, 9'), [0, 1, 2, 3, 8])
  assert.deepEqual(parseBlockRange('1-4, 9', 100), [0, 1, 2, 3, 8])
  assert.deepEqual(parseBlockRange('  ', 100), [])
  assert.deepEqual(parseBlockRange('4,2,4', 100), [1, 3])
})
