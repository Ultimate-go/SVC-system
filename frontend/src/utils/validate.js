/**
 * 前端表单校验 —— 与后端 schemas.py 保持同一套约束。
 * （后端只认这些，前端要做同样的约束，否则提交上去才被 422 拒掉。）
 */

/** 用户名：1–64 字符，正则 ^[A-Za-z0-9_.-]+$（与后端一致）。 */
export function validUsername(v) {
  const s = String(v ?? '')
  return s.length >= 1 && s.length <= 64 && /^[A-Za-z0-9_.-]+$/.test(s)
}

export function validDisplayName(v) {
  const s = String(v ?? '')
  return s.length <= 64
}

export function validRole(v) {
  return v === 'admin' || v === 'user'
}

export function validPassword(v) {
  const s = String(v ?? '')
  return s.length >= 1 && s.length <= 256
}

/**
 * 解析「全局下标」输入，支持 0-3, 8 这种区间语法。
 * 返回排序去重的数组；语法错抛 Error（带行号信息）。
 */
export function parseIndexRange(text) {
  const s = String(text ?? '').trim()
  if (!s) return []
  const out = new Set()
  for (const part of s.split(',')) {
    const p = part.trim()
    if (!p) continue
    if (/^\d+$/.test(p)) {
      out.add(Number(p))
    } else if (/^(\d+)\s*-\s*(\d+)$/.test(p)) {
      const m = p.match(/^(\d+)\s*-\s*(\d+)$/)
      const a = Number(m[1])
      const b = Number(m[2])
      if (a > b) throw new Error(`区间写反了：${p}`)
      for (let i = a; i <= b; i++) out.add(i)
    } else {
      throw new Error(`下标写法不对：${p}`)
    }
  }
  return [...out].sort((a, b) => a - b)
}
