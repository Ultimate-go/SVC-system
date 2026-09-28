/**
 * 验证结果的标题/副标题单元测试 —— 用 Node 自带的 node --test。
 *
 * 跑法：cd frontend; npm test
 *
 * 这两行文案看着不起眼，但它们是真出事时人第一眼看到的东西。
 * 这里钉住的是一条踩过的坑：
 * 一开始标题只写 code_name，于是「密文被换、分量没动」这种情形
 * （承诺层确实通过、是块哈希层抓住的）会显示成自相矛盾的「验证失败 · OK」；
 * 而修它的第一版又把条件写反了（标题说失败、副标题却搬来「验证通过」）。
 */

import { strict as assert } from 'node:assert'
import { describe, it } from 'node:test'

import { verifyFailDetail, verifyFailTitle } from '../src/utils/format.js'

/** 承诺层失败的例子（BAD_LAMBDA）。 */
const lambdaFail = {
  ok: false,
  hash_layer_ok: false,
  verify: { ok: false, code: 3, code_name: 'BAD_LAMBDA', message: 'Λ_I 校验失败：…' },
}

/** ★ 最容易被写错的那一格：密文被换、分量没动。 */
const hashLayerFail = {
  ok: false,
  hash_layer_ok: false,
  verify: { ok: true, code: 0, code_name: 'OK', message: '验证通过' },
}

const pass = {
  ok: true,
  hash_layer_ok: true,
  verify: { ok: true, code: 0, code_name: 'OK', message: '验证通过' },
}

describe('verifyFailTitle（失败标题必须说清是哪一层）', () => {
  it('通过时就是「验证通过」', () => {
    assert.equal(verifyFailTitle(pass), '验证通过')
  })

  it('承诺层失败：标题带环节名', () => {
    assert.equal(verifyFailTitle(lambdaFail), '验证失败 · BAD_LAMBDA')
  })

  it('★ 承诺层过了、整体失败：说「块哈希层」，不能显示成「验证失败 · OK」', () => {
    const t = verifyFailTitle(hashLayerFail)
    assert.equal(t, '验证失败 · 块哈希层（承诺层是过的）')
    assert.ok(!t.includes('OK'), `标题里不该出现 OK：${t}`)
  })

  it('没有结果时给空串（模板里不会渲染出半个标题）', () => {
    assert.equal(verifyFailTitle(null), '')
  })
})

describe('verifyFailDetail（副标题不能自相矛盾）', () => {
  it('通过时给算法核的原话', () => {
    assert.equal(verifyFailDetail(pass), '验证通过')
  })

  it('承诺层失败时给算法核给的环节解释', () => {
    assert.equal(verifyFailDetail(lambdaFail), 'Λ_I 校验失败：…')
  })

  it('★ 第二层失败时不能搬「验证通过」当副标题', () => {
    const d = verifyFailDetail(hashLayerFail)
    assert.notEqual(d, '验证通过')
    assert.ok(d.includes('密文'), `副标题要说清是密文被换：${d}`)
  })

  it('没有结果时给空串', () => {
    assert.equal(verifyFailDetail(null), '')
  })
})
