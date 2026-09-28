/**
 * 耗时 / 阶段相关的工具。
 *
 * ★ 本模块是纯函数：不 import Vue、不碰 DOM，否则 Node 测试挂。
 *
 * 后端几乎每个写操作的响应都带 timings 字段：
 *   { total_ms: number, stages: [{ stage: string, ms: number, count: number }] }
 * stages 的顺序就是真实执行顺序（后端 Stopwatch 按第一次出现排）。
 *
 * ★ 一条纪律：timings 不要编百分比。进行中只显示不定量动画 + 真实已用时间；
 *   完成后再用 StageTimeline 画阶段条。这里只做「真实数据」的格式化，不造假。
 */

/** 阶段名 → 颜色 tone（用于阶段条）。按语义归类，不按顺序。 */
export function stageColor(stageName) {
  const s = String(stageName || '')
  if (s.includes('加密') || s.includes('封装') || s.includes('SM4') || s.includes('PBKDF2') || s.includes('密钥')) {
    return 'warn'
  }
  if (s.includes('承诺') || s.includes('commit') || s.includes('分量') || s.includes('模幂')) {
    return 'accent'
  }
  if (s.includes('分发') || s.includes('推') || s.includes('网络') || s.includes('落库') || s.includes('取回') || s.includes('补推')) {
    return 'accent2'
  }
  if (s.includes('验证') || s.includes('verify') || s.includes('自检') || s.includes('哈希')) {
    return 'ok'
  }
  return 'muted'
}

/** 毫秒 → 可读字符串。 */
export function formatMs(ms) {
  const v = Number(ms || 0)
  if (v < 1) return `${v.toFixed(1)} ms`
  if (v < 1000) return `${v.toFixed(1)} ms`
  if (v < 60_000) return `${(v / 1000).toFixed(2)} s`
  return `${(v / 60_000).toFixed(2)} min`
}

/** 百分比（part / total），total 为 0 时返回 0，钳在 0~100。 */
export function pctOf(part, total) {
  const p = Number(part || 0)
  const t = Number(total || 0)
  if (t <= 0) return 0
  const pct = (p / t) * 100
  return Math.max(0, Math.min(100, pct))
}

/**
 * 把后端 timings 构造成渲染友好的结构。
 *
 * 输入：{ total_ms, stages: [{ stage, ms, count }] }
 * 输出：{ total_ms, stages: [{ stage, ms, count, pct, tone }], max_ms }
 *
 * pct 以「各段之和」为分母算占比（而不是 total_ms），
 * 因为 total_ms 会略大于各段之和（差额是埋点没覆盖到的零碎），
 * 强行按 total 摊会把差额摊进某一段 —— 那是撒谎。max_ms 供归一化条长用。
 */
export function buildPerf(timings) {
  if (!timings) return { total_ms: 0, stages: [], max_ms: 0, sum_ms: 0 }
  const stages = Array.isArray(timings.stages) ? timings.stages : []
  const total_ms = Number(timings.total_ms || 0)
  const sum_ms = stages.reduce((a, s) => a + Number(s.ms || 0), 0)
  const max_ms = stages.reduce((a, s) => Math.max(a, Number(s.ms || 0)), 0)
  const norm = stages.map((s) => ({
    ...s,
    ms: Number(s.ms || 0),
    count: Number(s.count || 1),
    pct: sum_ms > 0 ? (Number(s.ms || 0) / sum_ms) * 100 : 0,
    tone: stageColor(s.stage),
  }))
  return { total_ms, stages: norm, max_ms, sum_ms }
}

/** 从「可能是 axios 响应、也可能是裸数据」里取出 data。 */
export function unwrapAxios(resp) {
  if (resp && typeof resp === 'object' && 'data' in resp) return resp.data
  return resp
}

/**
 * 计时执行一个同步函数（或返回 Promise），返回 { result, ms }。
 * 用 performance.now()（单调时钟）。
 */
export function timed(fn) {
  const t0 = typeof performance !== 'undefined' ? performance.now() : Date.now()
  const out = fn()
  const t1 = typeof performance !== 'undefined' ? performance.now() : Date.now()
  return { result: out, ms: t1 - t0 }
}

/** 人类可读的耗时（用于「已用时间」等场景）。 */
export function humanMs(ms) {
  const v = Number(ms || 0)
  if (v < 1000) return `${Math.round(v)} ms`
  if (v < 60_000) return `${(v / 1000).toFixed(1)} s`
  return `${Math.round(v / 1000)} s`
}
