/**
 * 审计流水页「折叠」逻辑的回归测试。
 *
 * 这里测的是**与 Vue 无关的纯逻辑**：展开集合的增删语义、以及
 * "这一页是否全展开"。之所以值得单测，是因为这几个坑都很隐蔽：
 *
 *   1. `Set` 在原地增删**不会**触发 Vue 的响应式（浅层 ref 追踪不到内部变更），
 *      必须换一个新 Set 再赋回。写错了的表现是"点了没反应"，而不是报错。
 *   2. 展开状态用 `id` 而不是下标 —— 翻页后下标会串味（每页都有"第 3 行"）。
 *   3. 没有可展开内容的行（既无 detail 又无备注）不该进集合 ——
 *      否则「全部展开」按钮的"全展开了吗"判断会被这些空行永远卡住。
 */
import { strict as assert } from 'node:assert'
import { describe, it } from 'node:test'

// ---- 被测逻辑：从 index.vue 里原样搬过来的最小实现 ----
// （保持与源文件一致；源文件那边有 Vue 的 ref 包装，语义完全相同）

function hasMore(r) {
  return !!(r.detail || r.remark)
}

function toggle(expanded, r) {
  const next = new Set(expanded)
  if (next.has(r.id)) next.delete(r.id)
  else next.add(r.id)
  return next
}

function allExpandedOf(expanded, rows) {
  const list = rows.filter(hasMore)
  if (!list.length) return false
  return list.every((r) => expanded.has(r.id))
}

function toggleAllOf(expanded, rows) {
  const next = new Set(expanded)
  if (allExpandedOf(expanded, rows)) {
    for (const r of rows) next.delete(r.id)
  } else {
    for (const r of rows) if (hasMore(r)) next.add(r.id)
  }
  return next
}

/** 翻页后把不在本页的 id 滤掉（对应源文件的 pruneExpanded）。 */
function prune(expanded, rows) {
  const alive = new Set(rows.map((r) => r.id))
  const next = new Set()
  for (const id of expanded) if (alive.has(id)) next.add(id)
  return next
}

/**
 * 被拒记录默认展开（对应源文件的 autoExpandDenied）。
 * 返回新的 `{ expanded, autoExpanded }`。
 *
 * ★ 核心语义：只对**首次出现**的行生效 —— 已在 autoExpanded 里的 id 不再动。
 *   否则用户手动收起后，重查/翻页回来又会被强行撑开。
 */
function autoExpandDenied(expanded, autoExpanded, rows) {
  const opened = autoExpanded
  let exp = null
  let seen = null
  for (const r of rows) {
    if (r.ok || !hasMore(r) || opened.has(r.id)) continue
    if (!exp) exp = new Set(expanded)
    exp.add(r.id)
    if (!seen) seen = new Set(opened)
    seen.add(r.id)
  }
  return {
    expanded: exp ?? expanded,
    autoExpanded: seen ?? autoExpanded,
  }
}

const row = (id, detail = '', remark = '') => ({ id, detail, remark })
/** 带 ok 标志的行 —— 被拒 = ok:false。 */
const deniedRow = (id, detail = '拒绝原因') => ({ id, ok: false, detail, remark: '' })
const okRow = (id, detail = '') => ({ id, ok: true, detail, remark: '' })

describe('折叠：展开集合的增删', () => {
  it('toggle 一个没有展开的 id → 加上；再 toggle → 去掉', () => {
    const r = row(7)
    let e = new Set()
    e = toggle(e, r)
    assert.equal(e.has(7), true)
    e = toggle(e, r)
    assert.equal(e.has(7), false)
  })

  it('★ 必须换新 Set（原地改不会触发响应式）', () => {
    const r = row(7)
    const before = new Set()
    const after = toggle(before, r)
    assert.notEqual(before, after, '应当返回一个新的 Set 实例')
    assert.equal(before.has(7), false, '原集合不能被就地改动')
    assert.equal(after.has(7), true)
  })

  it('互不影响：展开 A 不会顺手展开 B', () => {
    let e = new Set()
    e = toggle(e, row(1))
    e = toggle(e, row(2))
    assert.deepEqual([...e].sort(), [1, 2])
    e = toggle(e, row(1))
    assert.deepEqual([...e], [2])
  })
})

describe('折叠：是否"全展开"', () => {
  const rows = [row(1, '原因'), row(2), row(3, '', '备注')] // 2 号无可展开内容

  it('空集合 → 不是全展开', () => {
    assert.equal(allExpandedOf(new Set(), rows), false)
  })

  it('★ 无内容的行不该卡住"全展开"判断', () => {
    // 只展开 1 和 3（2 号没有内容可展开）就应当算"全展开"
    assert.equal(allExpandedOf(new Set([1, 3]), rows), true)
  })

  it('漏了有内容的那条 → 不是全展开', () => {
    assert.equal(allExpandedOf(new Set([1]), rows), false)
  })

  it('一行都没有可展开内容 → 不是全展开（按钮也不该显示）', () => {
    assert.equal(allExpandedOf(new Set(), [row(1), row(2)]), false)
  })
})

describe('折叠：全部展开 / 全部收起', () => {
  const rows = [row(1, '原因'), row(2), row(3, '', '备注')]

  it('全部展开 → 只收进**有内容**的那几条', () => {
    const e = toggleAllOf(new Set(), rows)
    assert.deepEqual([...e].sort(), [1, 3], '2 号没有内容，不该进集合')
  })

  it('已是全展开 → 再点一次变成全部收起', () => {
    const e = toggleAllOf(new Set([1, 3]), rows)
    assert.deepEqual([...e], [], '应当清空')
  })

  it('收起时连"手动单独展开过"的也一起收掉', () => {
    const e = toggleAllOf(new Set([1, 2, 3]), rows)
    assert.deepEqual([...e], [])
  })
})

describe('折叠：翻页后的剪枝', () => {
  it('★ 换页后把不在本页的 id 滤掉（否则集合无限涨）', () => {
    const e = new Set([1, 2, 3, 4])
    const page2 = [row(3), row(4), row(5)]
    assert.deepEqual([...prune(e, page2)].sort(), [3, 4])
  })

  it('★ 翻回来时不会"自己又展开了"—— 剪掉的不会复活', () => {
    // 第 1 页展开 1、2；翻到第 2 页（只剩 3、4）；再翻回第 1 页
    let e = new Set([1, 2])
    e = prune(e, [row(3), row(4)])
    assert.deepEqual([...e], [], '离开第 1 页后 1、2 的展开状态已丢失')
    e = prune(e, [row(1), row(2)])
    assert.deepEqual([...e], [], '翻回来不该自动恢复展开')
  })

  it('本页都在 → 保持不动', () => {
    const e = new Set([1, 2])
    assert.deepEqual([...prune(e, [row(1), row(2), row(3)])].sort(), [1, 2])
  })
})

describe('折叠：被拒记录默认展开', () => {
  it('★ 被拒且有内容的行 → 首次出现即自动展开', () => {
    const rows = [okRow(1, '成功详情'), deniedRow(2)]
    const { expanded } = autoExpandDenied(new Set(), new Set(), rows)
    assert.deepEqual([...expanded], [2], '只展开被拒的那条')
  })

  it('成功的行不自动展开（哪怕有 detail）', () => {
    const rows = [okRow(1, '成功详情'), okRow(2, '别的详情')]
    const { expanded, autoExpanded } = autoExpandDenied(new Set(), new Set(), rows)
    assert.equal(expanded.size, 0)
    assert.equal(autoExpanded.size, 0, '成功行不该留下痕迹')
  })

  it('被拒但没有可展开内容 → 不进集合（否则箭头都没有还占个展开位）', () => {
    const rows = [{ id: 3, ok: false, detail: '', remark: '' }]
    const { expanded, autoExpanded } = autoExpandDenied(new Set(), new Set(), rows)
    assert.equal(expanded.size, 0)
    assert.equal(autoExpanded.size, 0)
  })

  it('★★ 用户手动收起后，重查不会又把它撑开（痕迹生效）', () => {
    const rows = [deniedRow(2)]
    // 第一次加载：自动展开
    let st = autoExpandDenied(new Set(), new Set(), rows)
    assert.deepEqual([...st.expanded], [2])

    // 用户手动收起
    const afterManual = new Set(st.expanded)
    afterManual.delete(2)
    assert.equal(afterManual.size, 0)

    // 再次加载（同一行又回来了）—— 不该复展开
    st = autoExpandDenied(afterManual, st.autoExpanded, rows)
    assert.equal(st.expanded.size, 0, '已经在痕迹里，不能再自动展开')
  })

  it('★ 换到新的一页：新出现的被拒行照常自动展开', () => {
    // 第 1 页 {2} 已自动展开并留下痕迹
    let st = autoExpandDenied(new Set(), new Set(), [deniedRow(2)])
    assert.deepEqual([...st.expanded], [2])

    // 翻到第 2 页：prune 清掉痕迹（2 不在本页了），新页的被拒行 7 应当自动展开
    const page2 = [okRow(5), deniedRow(7)]
    const prunedExpanded = prune(st.expanded, page2)
    const prunedAuto = prune(st.autoExpanded, page2)
    st = autoExpandDenied(prunedExpanded, prunedAuto, page2)
    assert.deepEqual([...st.expanded], [7])
  })

  it('★ 已知取舍：离开页面再回来，被拒行会再自动展开一次', () => {
    // 第 1 页：2 自动展开 → 用户手动收起 → 去第 2 页 → 再翻回第 1 页
    let st = autoExpandDenied(new Set(), new Set(), [deniedRow(2)])
    const manual = new Set(st.expanded)
    manual.delete(2) // 用户手动收起

    const page2 = [deniedRow(7)]
    let e = prune(manual, page2)
    let a = prune(st.autoExpanded, page2) // ← 痕迹随 prune 一起清掉了
    st = autoExpandDenied(e, a, page2)

    // 翻回第 1 页：2 已经是"没见过的新行"，于是又自动展开
    e = prune(st.expanded, [deniedRow(2)])
    a = prune(st.autoExpanded, [deniedRow(2)])
    st = autoExpandDenied(e, a, [deniedRow(2)])

    assert.deepEqual([...st.expanded], [2], '会再展一次（痕迹已随 prune 清掉）')
    // 说明：这是**刻意接受的取舍**——
    //   · prune 的职责是"离开这一页就忘掉"，否则集合会随翻页无限膨胀；
    //   · 想跨页记住「用户手动收起过」需要一份全局持久化的"已看过"集合
    //     （甚至要落 localStorage），对一个审计页来说代价不值。
    //   · 而且这个"副作用"是**良性**的：被拒记录本来就是希望被看见的那批。
  })

  it('多条被拒 → 全部自动展开，且互不影响', () => {
    const rows = [deniedRow(1), okRow(2), deniedRow(3)]
    const { expanded, autoExpanded } = autoExpandDenied(new Set(), new Set(), rows)
    assert.deepEqual([...expanded].sort(), [1, 3])
    assert.deepEqual([...autoExpanded].sort(), [1, 3])
  })

  it('★ 必须换新实例（原地改不触发响应式）', () => {
    const before = new Set()
    const { expanded } = autoExpandDenied(before, new Set(), [deniedRow(2)])
    assert.notEqual(before, expanded, '应当返回新的 Set 实例')
    assert.equal(before.size, 0, '原集合不能被就地改动')
  })

  it('没有可自动展开的行 → 原集合原样返回（不做无谓的克隆）', () => {
    const before = new Set([1])
    const auto = new Set([1])
    const { expanded, autoExpanded } = autoExpandDenied(before, auto, [okRow(9)])
    assert.equal(expanded, before, '同一实例，没触发无谓的响应式更新')
    assert.equal(autoExpanded, auto)
  })
})
