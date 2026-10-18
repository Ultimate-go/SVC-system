/**
 * 界面上**由前端拥有**的那些句子。
 *
 * 为什么单开一份：这些句子以前由后端拼好随接口下发。后果是**改一个字都要重启
 * 后端** —— 而它们其实一个字都不依赖运行时状态（依赖的是数字，而数字照样在
 * 响应里）。所以从 2026-10-16 起：
 *
 * * 后端只回**数据**（计数、名单、端口号、毫秒数…）；
 * * 句子在这里拼。改措辞 = 改这个文件，前端热更新即时生效。
 *
 * ★ 与 `data/measured.js` 的分工：那份是**方案常量**（实测数字、算例），
 *   这份是**界面文案**（谁把数字说成一句什么话）。两者都放前端，改都不必重启后端。
 * ★ 这里不许出现 Markdown 记号（`**`、反引号），见 `docs/文案与注释规范.md` 第三节。
 * ★ 句子都是纯函数输出：输入是响应里的字段，**不读组件状态** ——
 *   于是能被 `frontend/tests/copy.test.js` 直接钉住。
 */

/* ------------------------------------------------------------------ *
 * 切块顾问（POST /api/plan）
 * ------------------------------------------------------------------ */

/** 估算系数的来源。后端只回 `measured_per_block_ms`（每块毫秒数）。 */
export const PLAN_SOURCE =
  'scripts/bench_blocksize.py 实测（本机，2026-09-26；单进程模式，只含本地封装与承诺）'

/** 估算系数的口径。界面必须跟数字一起摆出来，不然就是把估算说成实测。 */
export const PLAN_SCOPE =
  '只含协调者本地几步（切块 / SM3 / SM4 / ECIES 封装 / 承诺）；' +
  '不含把密文推给各存储节点的分发（跨进程部署下分发才是大头）'

/**
 * 「为什么推荐这一档」。
 *
 * @param {object} plan `POST /api/plan` 的响应（`prefer` / `segment_bytes` /
 *   `blocks` / `est_upload_ms` / `est_verify_ms` / `measured_per_block_ms`）。
 * @returns {string}
 */
export function planWhy(plan) {
  if (!plan || !plan.ok) return ''
  const seg = plan.segment_bytes
  const blocks = plan.blocks
  const up = plan.est_upload_ms
  const ver = plan.est_verify_ms
  const per = plan.measured_per_block_ms || {}
  if (plan.prefer === 'finer_updates') {
    return (
      `粒度更细的一档：在允许范围内挑最小的可行块 ${seg} 字节，切 ${blocks} 块。` +
      `代价是块数多、上传与验证更慢（本地那几步估算 ${up} ms / ${ver} ms），` +
      `也更占全局位置。`
    )
  }
  return (
    `块数最少的一档：块越大，每块的固定开销摊得越薄。${seg} 字节一块，共 ${blocks} 块，` +
    `本地那几步估算上传 ${up} ms、一次全量验证 ${ver} ms（每块约 ${per.upload} ms / ` +
    `${per.verify} ms，实测）。这几个数不含把密文推给各存储节点的分发，所以只适合` +
    `比较切法；上传到底要多久，看上传完成后那条阶段耗时条。块大只影响改一块的粒度，` +
    `不影响验证结果，证据大小也与块数无关。`
  )
}

/** 一档都不可用时的理由（`ok=false`；正常部署走不到）。 */
export function planReason() {
  return '没有任何可用的切法（块大小候选为空？）—— 检查部署里的块大小范围。'
}

/* ------------------------------------------------------------------ *
 * 服务器台数（/api/admin/deploy）
 * ------------------------------------------------------------------ */

/**
 * 台数卡片顶上那条提示。
 *
 * @param {object} info `GET /api/admin/deploy` 的响应。`info.note` 是上次保存时
 *   写进 `nodes/deploy.json` 的**记录**（不是文案，照原样显示）。
 */
export function deployNotice(info) {
  if (!info) return ''
  if (info.note) {
    return info.restart_required ? `${info.note} 点「立刻重启」让 ${info.node_count} 台生效。` : info.note
  }
  if (!info.restart_required) return ''
  const ids = (info.running_node_ids || []).join(', ')
  return (
    `服务器台数已从 ${info.running_count} 台改成 ${info.node_count} 台，重启后生效 —— ` +
    `现在跑的还是 ${info.running_count} 台（${ids}）。`
  )
}

/**
 * 保存台数之后那一句（`PUT /api/admin/deploy`）。
 *
 * @param {object} data 响应：`node_count` / `running_count` / `requested` /
 *   `clamped` / `redistribute`。
 */
export function deploySavedMessage(data) {
  if (!data) return ''
  const want = data.node_count
  const running = data.running_count
  const moved = data.redistribute
  let msg
  if (moved) {
    msg = `服务器数量减少，文件块已重新分配：${moved.moved_blocks} 块已铺到剩下的 ${want} 台上。`
    if ((moved.lost || []).length) {
      msg += `\n另有 ${moved.lost.length} 块没能搬走 —— 每一份副本都联系不上。`
    }
    msg += `\n点「立刻重启」让这 ${want} 台生效。`
  } else if (want === running) {
    msg = `台数没变（还是 ${running} 台）。`
  } else {
    msg = `台数已从 ${running} 台改成 ${want} 台，重启后生效（现在跑的还是 ${running} 台）。\n点「立刻重启」即可。`
  }
  if (data.clamped) msg += `\n（${data.requested} 超出台数范围，取 ${want}）`
  return msg
}

/**
 * 「补齐不足的副本」之后那一句（`POST /api/admin/deploy/redistribute`）。
 *
 * @param {object} data 响应：`moved_blocks` / `moved_copies` / `lost`（搬不动的下标）。
 */
export function redistributeMessage(data) {
  if (!data) return ''
  let msg = `补了 ${data.moved_blocks} 块（${data.moved_copies} 份副本）`
  const lost = data.lost || []
  if (lost.length) msg += `；另有 ${lost.length} 块搬不动：[${lost.slice(0, 12).join(', ')}]`
  return msg
}

/* ------------------------------------------------------------------ *
 * 端口（/api/admin/ports）
 * ------------------------------------------------------------------ */
/**
 * 「保存下来的那一套」与「现在跑着的那一套」的差异清单。
 *
 * 两处都用它：卡片上那条提示、保存之后那句成功提示。
 * @returns {string[]} 形如 `后端 8000 → 8100`；空数组 = 没变。
 */
export function portsDiffs(info) {
  if (!info) return []
  const running = info.running || {}
  const out = []
  for (const [key, label] of [
    ['backend', '后端'],
    ['frontend', '前端'],
  ]) {
    const now = running[key]
    if (now != null && now !== info[key]) out.push(`${label} ${now} → ${info[key]}`)
  }
  for (const [nid, port] of Object.entries(info.nodes || {})) {
    const now = (running.nodes || {})[nid]
    if (now != null && now !== port) out.push(`${nid} ${now} → ${port}`)
  }
  return out
}

/**
 * 「改了端口还没重启」那条提示（只在真要重启时显示）。
 *
 * @param {object} info `GET /api/admin/ports` 的响应。
 */
export function portsNotice(info) {
  // ★ 没到“要重启”的状态就不该有这条提示 —— 以前这个判断在调用方，
  //   但那是**句子**自己的前提条件，放在这里才不会被漏掉。
  if (!info || !info.restart_required) return ''
  const diffs = portsDiffs(info)
  const body = diffs.length ? `：${diffs.join('；')}。` : '。'
  const runningFrontend = (info.running || {}).frontend
  let tail
  if (runningFrontend != null && runningFrontend !== info.frontend) {
    tail =
      `重启后前端在 http://127.0.0.1:${info.frontend} —— ` +
      `现在这个页面会失效，重启器会自动在新地址开一个。`
  } else {
    tail = '重启期间页面会连不上，起来之后刷新一下即可（要重新登录一次）。'
  }
  return `端口改了，还没重启${body}${tail}`
}

/**
 * 读配置时**替你改过**的那几个端口（同一个号只能给一样东西用）。
 *
 * @param {Array<{node: string, was: number, now: number}>} items 响应里的 `auto_fixed`。
 */
export function portsAutoFixed(items) {
  return (items || [])
    .map((x) => `${x.node} 的端口 ${x.was} 和别的端口重复了，改用 ${x.now}`)
    .join('；')
}

/**
 * 被别的程序占着的那几个。
 *
 * @param {Array<{id: string, port: number}>} items 响应里的 `occupied`。
 */
export function portsOccupiedWarning(items) {
  const bad = items || []
  if (!bad.length) return ''
  const shown = bad
    .slice(0, 10)
    .map((x) => `${x.id} ${x.port}`)
    .join('、')
  const more = bad.length > 10 ? `（共 ${bad.length} 个）` : ''
  return (
    `${shown}${more} 现在有别的程序在占用 —— 重启时这几样会起不来。` +
    `可以换一个端口，或者先把占用它的程序停掉。`
  )
}

/** 保存端口之后那一句（`PUT /api/admin/ports`）。 */
export function portsSavedMessage(info) {  if (!info) return ''
  const diffs = portsDiffs(info)
  let msg = diffs.length
    ? `端口已保存：${diffs.join('；')}。重启后生效。`
    : '端口没变，配置已按当前值写回。'
  if (diffs.some((d) => d.includes('前端'))) {
    msg +=
      `\n重启后前端在 http://127.0.0.1:${info.frontend}` +
      ` —— 这个地址会失效，重启器会自动在新地址开一个页面。`
  }
  return msg
}

/**
 * 一条端口冲突。
 *
 * @param {{kind: 'backend'|'frontend'|'node', who: string, with: string, port: number}} c
 *   响应里的 `conflicts` / `params.conflicts` 元素（后端只回数据，见
 *   `backend/config.py::check_port_conflicts`）。
 */
export function portConflictLine(c) {
  if (!c) return ''
  return c.kind === 'node'
    ? `${c.who} 的 ${c.port} 与「${c.with}」重复`
    : `${c.who}与「${c.with}」填了同一个端口 ${c.port}`
}

/** 冲突 409 的正文（后端那句只给 curl 看）。 */
export function portConflictDetail(items) {
  const lines = (items || []).map(portConflictLine).filter(Boolean)
  if (!lines.length) return ''
  return (
    '同一个端口只能给一样东西用 —— 下面这几处撞了，重启时后起的那个一定起不来：\n  ' +
    lines.join('\n  ') +
    '\n\n改掉其中一方再保存。'
  )
}

/* ------------------------------------------------------------------ *
 * 故障演练的影响面（GET /api/admin/fault-drill）
 * ------------------------------------------------------------------ */

/**
 * 把演练的数字翻成业务后果。
 *
 * @param {object} drill 响应：`faulty`（故障名单）与 `impact`
 *   （`lost_count` / `degraded_blocks` / `files_affected`）。
 */
export function faultImpactNote(drill) {
  if (!drill) return ''
  const impact = drill.impact || {}
  const tail = '（本演练不删数据，一个字节都不动；点击「恢复」即可全部复原。）'
  if (!(drill.faulty || []).length) return '无故障：全部存储节点在线。'
  const lost = impact.lost_count || 0
  const degraded = impact.degraded_blocks || 0
  if (lost) {
    return (
      `影响评估：${lost} 个分块将永久丢失，其全部副本均位于故障节点，涉及 ` +
      `${(impact.files_affected || []).length} 份文件。` +
      `成因是「副本数不足」与「不可逆故障」同时成立。${tail}`
    )
  }
  if (degraded) {
    return (
      `${degraded} 个分块的主副本不可用，但其余副本仍然在线 —— ` +
      `读取与验证会自动改用其余副本，当前功能不受影响。` +
      `这正是多副本的意义：单节点故障不导致服务中断。${tail}`
    )
  }
  return `故障节点上没有任何在用的分块（可能未分配到分块）。业务功能不受影响。${tail}`
}

/* ------------------------------------------------------------------ *
 * 删除用户（DELETE /api/admin/users/{id}）
 * ------------------------------------------------------------------ */

/**
 * 删号之后那段说明。
 *
 * @param {object} data 响应：`deleted` / `files_deleted` / `blocks_deleted`。
 */
export function userDeletedWarning(data) {
  if (!data) return ''
  const name = data.deleted
  const files = data.files_deleted
  const blocks = data.blocks_deleted
  return (
    `${name} 已删除，用户名可以重新使用了。\n\n` +
    `他名下的 ${files} 份文件（共 ${blocks} 块）已随账号一起删除：\n` +
    `  · 文件不再出现在列表里，也不能再被验证；\n` +
    `  · 各存储节点上对应的分块份额已一并清除，密文与块密钥均不可恢复。\n\n` +
    `重新建一个同名的 ${name} 是一个全新的账号，不会继承任何东西。`
  )
}
