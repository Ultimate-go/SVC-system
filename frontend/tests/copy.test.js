/**
 * 界面文案（`src/data/copy.js`）与错误码表（`src/data/errors.js`）的钉子。
 *
 * ★ 为什么单开这一份：这些句子以前**在后端**拼（`backend/routers/*.py`、
 *   `backend/manager.py`），当时是 `scripts/verify_*.py` 去断言它们的措辞
 *   （例如“影响面必须说清不删数据”）。2026-10-16 起句子搬到前端 ——
 *   **断言也跟着搬到这里**，否则那几条保证就没人看着了。
 *
 * 这里只测纯函数：输入是接口响应里的数据，输出是句子。
 */

import test from 'node:test'
import assert from 'node:assert/strict'

import {
  PLAN_SOURCE,
  deployNotice,
  deploySavedMessage,
  faultImpactNote,
  planWhy,
  portConflictDetail,
  portConflictLine,
  portsAutoFixed,
  portsNotice,
  portsOccupiedWarning,
  redistributeMessage,
  userDeletedWarning,
} from '../src/data/copy.js'
import { apiErrorText } from '../src/data/errors.js'

/* ------------------------------------------------------------------ *
 * 故障演练影响面：两条**安全承诺**必须写在界面上
 * ------------------------------------------------------------------ */

test('影响面：无故障时明确说“在线”', () => {
  const note = faultImpactNote({
    faulty: [],
    impact: { lost_count: 0, degraded_blocks: 0, files_affected: [] },
  })
  assert.match(note, /在线/)
  assert.doesNotMatch(note, /永久丢失/)
})

test('影响面：多副本救场那一档说清“其余副本仍在线”', () => {
  const note = faultImpactNote({
    faulty: ['node-1'],
    impact: { lost_count: 0, degraded_blocks: 7, files_affected: [] },
  })
  assert.match(note, /其余副本仍然在线/)
  assert.match(note, /7 个分块/)
})

test('影响面：永久丢失那一档必须点明「副本数不足 + 不可逆故障」', () => {
  const note = faultImpactNote({
    faulty: ['node-1', 'node-2'],
    impact: {
      lost_count: 4,
      degraded_blocks: 0,
      files_affected: [{ owner: 'alice', file_key: 'mid', lost_blocks: 4 }],
    },
  })
  assert.match(note, /4 个分块将永久丢失/)
  assert.match(note, /不可逆故障/)
  assert.match(note, /1 份文件/)
})

test('★★ 影响面：故障那一档必须说清「本演练不删数据」（用户的安全承诺）', () => {
  const cases = [
    { faulty: ['n'], impact: { lost_count: 0, degraded_blocks: 1, files_affected: [] } },
    {
      faulty: ['n'],
      impact: { lost_count: 2, degraded_blocks: 0, files_affected: [{ lost_blocks: 2 }] },
    },
  ]
  for (const drill of cases) {
    assert.match(faultImpactNote(drill), /不删数据/, JSON.stringify(drill.impact))
    assert.match(faultImpactNote(drill), /点击「恢复」即可全部复原/)
  }
  // 无故障那一档没有“演练”可谈，只说在线（与后端原来那句一致）。
  assert.doesNotMatch(
    faultImpactNote({ faulty: [], impact: { lost_count: 0, degraded_blocks: 0 } }),
    /不删数据/,
  )
})

/* ------------------------------------------------------------------ *
 * 服务器台数
 * ------------------------------------------------------------------ */

test('台数提示：没改就不出提示', () => {
  assert.equal(
    deployNotice({ node_count: 3, running_count: 3, restart_required: false, note: '' }),
    '',
  )
})

test('台数提示：改了还没重启时说清“跑的还是几台”', () => {
  const note = deployNotice({
    node_count: 2,
    running_count: 3,
    running_node_ids: ['node-1', 'node-2', 'node-3'],
    restart_required: true,
    note: '',
  })
  assert.match(note, /服务器台数已从 3 台改成 2 台/)
  assert.match(note, /现在跑的还是 3 台（node-1, node-2, node-3）/)
})

test('台数提示：配置文件里那句记录照原样带出，并追加“立刻重启”', () => {
  const note = deployNotice({
    node_count: 2,
    running_count: 3,
    restart_required: true,
    note: '服务器台数已从 2 台改成 3 台',
  })
  assert.match(note, /^服务器台数已从 2 台改成 3 台/)
  assert.match(note, /点「立刻重启」让 2 台生效。/)
})

test('保存台数：减台报出搬了多少块，并提示重启', () => {
  const msg = deploySavedMessage({
    node_count: 2,
    running_count: 3,
    requested: 2,
    clamped: false,
    redistribute: { moved_blocks: 5, moved_copies: 6, lost: [] },
  })
  assert.match(msg, /文件块已重新分配：5 块/)
  assert.match(msg, /点「立刻重启」让这 2 台生效/)
  assert.doesNotMatch(msg, /没能搬走/)
})

test('保存台数：有搬不走的块要单独说一句，并带上被夹的原始值', () => {
  const msg = deploySavedMessage({
    node_count: 2,
    running_count: 3,
    requested: 99,
    clamped: true,
    redistribute: { moved_blocks: 5, moved_copies: 6, lost: [7, 8] },
  })
  assert.match(msg, /另有 2 块没能搬走/)
  assert.match(msg, /（99 超出台数范围，取 2）/)
})

test('保存台数：台数没变就说没变', () => {
  assert.equal(
    deploySavedMessage({ node_count: 3, running_count: 3, redistribute: null, clamped: false }),
    '台数没变（还是 3 台）。',
  )
})

test('补齐副本：报出块数与份数，搬不动的列出下标', () => {
  assert.equal(redistributeMessage({ moved_blocks: 3, moved_copies: 4, lost: [] }), '补了 3 块（4 份副本）')
  assert.match(
    redistributeMessage({ moved_blocks: 3, moved_copies: 4, lost: [9, 10] }),
    /；另有 2 块搬不动：\[9, 10\]/,
  )
})

/* ------------------------------------------------------------------ *
 * 端口
 * ------------------------------------------------------------------ */

const ports = (over = {}) => ({
  backend: 8000,
  frontend: 5173,
  nodes: { 'node-1': 9101 },
  running: { backend: 8000, frontend: 5173, nodes: { 'node-1': 9101 } },
  restart_required: false,
  ...over,
})

test('端口提示：没变就不出提示', () => {
  assert.equal(portsNotice(ports()), '')
})

test('端口提示：节点端口变了，逐条列出来', () => {
  const note = portsNotice(
    ports({
      nodes: { 'node-1': 9111 },
      running: { backend: 8000, frontend: 5173, nodes: { 'node-1': 9101 } },
      restart_required: true,
    }),
  )
  assert.match(note, /^端口改了，还没重启：node-1 9101 → 9111。/)
  assert.match(note, /重启期间页面会连不上/)
})

test('★ 端口提示：前端端口没变时**不能**说“这个页面会失效”', () => {
  const note = portsNotice(
    ports({
      nodes: { 'node-1': 9111 },
      running: { backend: 8000, frontend: 5173, nodes: { 'node-1': 9101 } },
      restart_required: true,
    }),
  )
  assert.doesNotMatch(note, /http:\/\/127\.0\.0\.1:5173/)
  assert.doesNotMatch(note, /这个页面会失效/)
})

test('端口提示：前端端口真变了才提新地址', () => {
  const note = portsNotice(
    ports({
      frontend: 5273,
      running: { backend: 8000, frontend: 5173, nodes: { 'node-1': 9101 } },
      restart_required: true,
    }),
  )
  assert.match(note, /http:\/\/127\.0\.0\.1:5273/)
  assert.match(note, /现在这个页面会失效/)
})

test('端口：读配置时替改过的要逐条说出来', () => {
  assert.equal(portsAutoFixed([]), '')
  assert.equal(
    portsAutoFixed([{ node: 'node-2', was: 9101, now: 9102 }]),
    'node-2 的端口 9101 和别的端口重复了，改用 9102',
  )
})

test('端口：被占用的名单最多列 10 个，超过就给出总数', () => {
  const items = Array.from({ length: 12 }, (_, i) => ({ id: `node-${i + 1}`, port: 9100 + i }))
  const text = portsOccupiedWarning(items)
  assert.match(text, /node-1 9100、node-2 9101/)
  assert.match(text, /（共 12 个）/)
  assert.match(text, /先把占用它的程序停掉/)
  assert.equal(portsOccupiedWarning([]), '')
})

test('端口冲突：节点那一条与前后端那一条措辞不同', () => {
  assert.equal(
    portConflictLine({ kind: 'node', who: 'node-1', with: '前端', port: 5000 }),
    'node-1 的 5000 与「前端」重复',
  )
  assert.equal(
    portConflictLine({ kind: 'backend', who: '后端', with: '前端', port: 5000 }),
    '后端与「前端」填了同一个端口 5000',
  )
  assert.equal(portConflictDetail([]), '')
  assert.match(
    portConflictDetail([{ kind: 'node', who: 'node-1', with: '前端', port: 5000 }]),
    /node-1 的 5000 与「前端」重复/,
  )
})

test('★ 端口冲突：409 的句子与本地拦下来那句必须一致（不许两处漂移）', () => {
  const conflicts = [{ kind: 'backend', who: '后端', with: '前端', port: 5000 }]
  assert.equal(apiErrorText({ code: 'ports.conflict', params: { conflicts } }), portConflictDetail(conflicts))
})

/* ------------------------------------------------------------------ *
 * 切块顾问
 * ------------------------------------------------------------------ */

const planRow = {
  ok: true,
  prefer: 'fewest_blocks',
  segment_bytes: 4096,
  blocks: 8,
  est_upload_ms: 106.4,
  est_verify_ms: 54.4,
  measured_per_block_ms: { upload: 13.3, verify: 6.8 },
}

test('切块顾问：块数最少那一档必须把数字与口径一起说清', () => {
  const why = planWhy(planRow)
  assert.match(why, /4096 字节一块，共 8 块/)
  assert.match(why, /106\.4 ms/)
  assert.match(why, /13\.3 ms \/ 6\.8 ms，实测/)
  assert.match(why, /不含把密文推给各存储节点的分发/)
})

test('切块顾问：粒度最细那一档说的是另一件事', () => {
  const why = planWhy({ ...planRow, prefer: 'finer_updates', segment_bytes: 1024, blocks: 32 })
  assert.match(why, /粒度更细的一档/)
  assert.match(why, /1024 字节，切 32 块/)
})

test('切块顾问：算不出切法时 why 为空（由 reason 那句接管）', () => {
  assert.equal(planWhy({ ok: false }), '')
  assert.equal(planWhy(null), '')
})

test('切块顾问：估算来源必须自带出处，不能把估算说成实测', () => {
  assert.match(PLAN_SOURCE, /bench_blocksize\.py/)
  assert.match(PLAN_SOURCE, /实测/)
})

/* ------------------------------------------------------------------ *
 * 删号
 * ------------------------------------------------------------------ */

test('删号说明：带上件数，并说清重建同名账号不等于原来的账号', () => {
  const text = userDeletedWarning({ deleted: 'alice', files_deleted: 3, blocks_deleted: 12 })
  assert.match(text, /^alice 已删除，用户名可以重新使用了。/)
  assert.match(text, /3 份文件（共 12 块）/)
  assert.match(text, /重新建一个同名的 alice 是一个全新的账号/)
})

/* ------------------------------------------------------------------ *
 * 错误码表
 * ------------------------------------------------------------------ */

test('错误码：查得到就用表里的措辞（不必重启后端）', () => {
  assert.equal(apiErrorText({ code: 'auth.no_token' }), '缺少令牌，请先登录')
  assert.equal(
    apiErrorText({ code: 'files.not_found', params: { file_id: 7 } }),
    '文件 7 不存在',
  )
})

test('错误码：查不到就回空串（调用方据此回退到后端的 detail）', () => {
  assert.equal(apiErrorText({}), '')
  assert.equal(apiErrorText({ code: 'nope.nope' }), '')
  assert.equal(apiErrorText({ detail: '后端原文' }), '')
  assert.equal(apiErrorText(null), '')
})

test('错误码：参数缺项不抛异常，只是回空串（宁可显示后端原文）', () => {
  assert.equal(apiErrorText({ code: 'files.not_found' }), '')
  assert.equal(apiErrorText({ code: 'ports.out_of_range', params: { label: '后端端口' } }), '')
})

test('错误码：库异常的原文透传没有码，界面就显示后端那句', () => {
  // 这些句子在后端由 `str(exc)` 直接给出（见 backend/errors.py 的“边界”）。
  assert.equal(apiErrorText({ code: 'files.conflict', params: {} }), '')
})
