/**
 * 后端错误码 → 界面上的中文。
 *
 * 分工（2026-10-16 起）：
 *
 * * 后端 `raise ApiError(状态码, "码", "后端自己那一句", **参数)` ——
 *   `detail` 里仍旧是**字符串**（curl / 脚本 / 日志 / 旧代码都照旧用），
 *   同时多带一个 `code` 与（可选的）`params`。见 `backend/errors.py`。
 * * 界面**优先**按 `code` 查这张表；查不到就退回后端的 `detail`
 *   （见 `api/client.js`）。于是改界面措辞只改这个文件 —— **不必重启后端**。
 *
 * ★ 边界：库异常的原文透传（各路由里的 `except Xxx as exc: … str(exc)`）**没有**
 *   码，它们照旧用后端原文。理由见 `backend/errors.py` 里那段说明。
 * ★ `params` 只放能安全公开的值（人名、端口号、数量、下标）。
 * ★ 这里不许出现 Markdown 记号；换行用 `\n`，前端按纯文本渲染
 *   （`ElMessage` / `el-alert` 收的是纯文本）。
 *
 * 新增一条的流程：后端 `raise ApiError(…, "area.thing", …, p=…)` →
 * 这里加一条 → `python scripts/check_error_codes.py` 比对两边（缺一条就报错）。
 */

/** 错误码 → `(params) => string`。 */
export const ERROR_TEXT = {
  /* ---------- 请求与兜底（backend/main.py） ---------- */
  'request.length_not_integer': (p) => `Content-Length 不是整数：${p.raw}`,
  'request.too_large': (p) => `请求体 ${p.declared} 字节超过上限 ${p.limit_mib} MiB`,
  'server.internal': () => '服务器内部错误',

  /* ---------- 身份与令牌（backend/deps.py、routers/auth.py） ---------- */
  'auth.no_token': () => '缺少令牌，请先登录',
  'auth.token_revoked': () => '这张令牌已被作废（已退出登录），请重新登录',
  'auth.user_gone': () => '用户不存在',
  'auth.account_disabled': () => '账号已被停用',
  'auth.admin_required': () => '需要管理员权限',
  'auth.bad_credentials': () => '用户名或口令错误',
  'auth.no_keypair': (p) =>
    `这个账号还没有密钥对（${p.reason}）—— 请管理员重建，或者跑 seed --reset`,
  'auth.key_unseal_failed': () =>
    '口令对了，但私钥解不开 —— 口令与私钥的包裹不同步（改口令时重新封装那一步没落地）。\n' +
    '请不要删除重建这个账号：那会换掉密钥对，此人名下的文件将永久无法解密。\n' +
    '正确修法：用旧口令解封私钥，再用当前口令重新封装 —— 私钥本身完好，文件一把都不会丢。',

  /* ---------- 用户管理（backend/routers/admin.py） ---------- */
  'users.exists': (p) => `用户 ${p.username} 已存在`,
  'users.not_found': () => '用户不存在',
  'users.privileged_only': () => '只有管理员能改角色或停用状态（改口令与显示名可以自己改）',
  'users.cannot_disable_self': () => '不能停用自己',
  'users.cannot_change_password': (p) =>
    `不能替 ${p.username} 改口令：他的私钥是用他自己的口令包的，` +
    `改了他的口令就把他手里的私钥永久弄丢（他以前的文件会全部解不开）。\n` +
    `  让他自己登录后改（管理员可以在「用户管理」里停用他）。`,
  'users.session_no_key': () =>
    '这次会话里没有你的私钥（默认模型：后端不持有）—— ' +
    '请用「浏览器改口令」：前端会在本地用新口令把私钥重新封装，再把密文交上来。\n' +
    '  （旧的 server_key=true 那条路仍然可用，但它会让服务端暂时拿到明文私钥。）',

  /* ---------- 审计（backend/routers/admin.py） ---------- */
  'audit.not_found': () => '这条流水不存在',
  'audit.remark_too_long': (p) => `备注最长 ${p.max_chars} 字`,

  /* ---------- 一致性自检 ---------- */
  'check.failed': (p) => `自检失败：${p.reason}`,

  /* ---------- 服务器台数（backend/routers/admin.py） ---------- */
  'deploy.capacity_loss': (p) => {
    const shown = p.indices.join('、')
    const more = p.lost_count > p.indices.length ? `…（共 ${p.lost_count} 块）` : ''
    const down = p.down.length ? p.down.join(', ') : '——'
    return (
      `把服务器从 ${p.from_count} 台减到 ${p.to_count} 台，但有 ${p.lost_count} 块现在搬不走：\n` +
      `  下标 ${shown}${more}\n` +
      `  它们的每一份副本都在联系不上的机器上（${down}）。\n` +
      `  现在收缩 = 这几块永久丢失（承诺里那把锁还在，内容没了）。\n` +
      `  正确做法是先把那几台机器起起来，再收缩 —— 那时它们就搬得动了。\n` +
      `  确实要带着这个损失继续，请在界面上勾选「我知道这几块会丢」。`
    )
  },
  'deploy.confirm_redistribute': (p) => {
    const remove = p.remove.length ? p.remove.join(', ') : '——'
    return (
      `把服务器从 ${p.from_count} 台减到 ${p.to_count} 台，需要重新分配 ${p.orphan_count} 块：\n` +
      `  这些块现在只在 ${remove} 上还留着副本。\n` +
      `  保存时会趁那几台还活着的时候，把内容和证据搬到 ${p.keep.join(', ')} 上 ——\n` +
      `  块内容、全局下标和承诺一个字都不变，不会丢。\n` +
      `  确认请再点一次（界面上会问你一句）。`
    )
  },
  'deploy.restart_script_missing': (p) => `找不到重启脚本：${p.path}（工程被挪过？）`,
  'deploy.restart_failed': (p) => `拉不起重启脚本：${p.reason}`,

  /* ---------- 端口（backend/routers/admin.py） ---------- */
  'ports.out_of_range': (p) => `${p.label}超出范围：${p.port}。允许 ${p.min}–${p.max}。`,
  'ports.conflict': (p) => {
    // ★ 这里的句子与 `data/copy.js::portConflictDetail` **同一份口径**：
    //   同一个功能的两个入口（保存前 409、保存后 409）必须说一样的话。
    const lines = (p.conflicts || []).map((c) =>
      c.kind === 'node'
        ? `${c.who} 的 ${c.port} 与「${c.with}」重复`
        : `${c.who}与「${c.with}」填了同一个端口 ${c.port}`,
    )
    if (!lines.length) return '有端口填重了 —— 同一个端口只能给一样东西用。'
    return (
      '同一个端口只能给一样东西用 —— 下面这几处撞了，重启时后起的那个一定起不来：\n  ' +
      lines.join('\n  ') +
      '\n\n改掉其中一方再保存。'
    )
  },

  /* ---------- 文件（backend/routers/files.py） ---------- */
  'files.not_found': (p) => `文件 ${p.file_id} 不存在`,
  'files.gone': () => '文件不存在',
  'files.empty': () => '文件为空',
  'files.too_large': (p) => `文件超过 ${p.limit_mb} MB 上限`,
  'files.bad_nodes': () =>
    'nodes 传了但没有一个有效的机器名（应当是逗号分隔，如 node-1,node-2）',
  'files.truncate_missing_drop': () =>
    'op=truncate 必须提供 drop_blocks（需截断的末尾分块数）',
  'files.not_owner_modify': () =>
    '仅文件所有者可以改写该文件：改写需要重新封装块密钥，' +
    '与上传属于同一权限级别（管理员亦不具备该权限）。\n' +
    '  注意：该限制不影响验证 —— 你仍可验证此文件的完整性。',
  'files.not_owner_delete': () =>
    '仅文件所有者可以删除该文件：该操作会把本文件的全部分块从所有节点上实际删除。\n' +
    '  注意：其他文件不受影响；可验证查询亦不受影响 —— 其他文件仍可正常验证。',
  'files.not_owner_replay': () => '仅文件所有者可以对该文件执行回滚演示',
  'files.not_owner_restore': () =>
    '仅文件所有者可以回退本文件的版本（回退会恢复旧密文与旧块密钥）。',
  'files.denied_not_owner': () =>
    '这不是你的文件，所以拿不到块密钥。' +
    '注意：你仍然可以验证它的完整性 —— 验证是公开的，解密才需要私钥。',
  'files.decrypt_needs_browser': () =>
    '本次会话在服务端没有你的私钥 —— 这是「默认模型」的行为：\n' +
    '  私钥在浏览器内用口令解封（登录响应中的 key_blob），服务端不解封、也不保存。\n' +
    '  解密请使用客户端路径：POST /api/files/{id}/cipher 取回密文，' +
    '由浏览器解封块密钥并执行 SM4 解密。\n' +
    '  （如需对比旧模型：登录时显式传入 server_key=true。）',
  'files.decrypt_denied': (p) =>
    `${p.reason}\n注意：该限制不影响验证 —— 这些分块仍可正常验证。`,
}

/**
 * 按错误码取中文。取不到（没这个码、或模板抛错）就回空串，
 * 调用方据此回退到后端的 `detail`。
 *
 * @param {{code?: string, params?: object}} data 响应体。
 * @returns {string}
 */
export function apiErrorText(data) {
  if (!data || typeof data !== 'object') return ''
  const make = ERROR_TEXT[data.code]
  if (typeof make !== 'function') return ''
  let text
  try {
    text = String(make(data.params || {}) || '')
  } catch {
    // 参数缺项不该把界面搞崩：没有把握就用后端原文。
    return ''
  }
  // ★ 拼出来带 `undefined` / `NaN` 就是**参数没给全**（模板里引用了不存在的字段），
  //   那种句子不能给用户看 —— 宁可退回后端的原文。
  if (/undefined|NaN/.test(text)) return ''
  return text
}
