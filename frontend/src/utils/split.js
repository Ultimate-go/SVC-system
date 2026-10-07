/**
 * 自动切块：按**文件大小**挑块大小（小文件小块，大文件大块）。
 *
 * ★★ 这是 `backend/config.py::auto_segment_bytes` 的**同口径副本**。
 *
 *   为什么允许"有副本"：上传前的"本次切法"必须**当场**显示出来（不能让用户
 *   先发一次请求才知道自己切多大）。所以前端要算一遍。
 *
 *   为什么不怕两边走偏：两侧的测试钉的是**同一张表** ——
 *   `scripts/verify_segment_ladder.py`（后端）与 `frontend/tests/split.test.js`
 *   （前端）。任何一边改了档位、另一边没跟上，就有一侧的测试变红。
 *   而且真正生效的值永远由**服务端**算（"自动"档上传时不带 `segment_bytes`），
 *   前端这份只影响那句预览文案。
 */

/** 默认阶梯（与后端 `SEGMENT_LADDER` 一致；正常由 `/api/status` 下发）。 */
export const SEGMENT_LADDER = [1024, 4096, 16384, 65536, 262144]

/** 自动切法下希望不超过的块数（与后端 `SEGMENT_TARGET_BLOCKS` 一致）。 */
export const SEGMENT_TARGET_BLOCKS = 128

/**
 * 挑一个块大小：在 `[minBytes, maxBytes]` 内，取阶梯里**最小**的那一档，
 * 使块数 `ceil(totalBytes / 档) ≤ targetBlocks`。
 *
 * @param {number} totalBytes 文件字节数。
 * @param {object} [opts]
 * @param {number[]} [opts.ladder] 阶梯（默认取 `/api/status.segment_ladder`）。
 * @param {number} [opts.targetBlocks] 目标块数上限。
 * @param {number} [opts.minBytes] 允许的最小块。
 * @param {number} [opts.maxBytes] 允许的最大块（阶梯全不够时也用它兜底）。
 * @returns {number} 每块的字节数。
 */
export function autoSegmentBytes(totalBytes, opts = {}) {
  const {
    ladder = SEGMENT_LADDER,
    targetBlocks = SEGMENT_TARGET_BLOCKS,
    minBytes = 64,
    maxBytes = 1048576,
  } = opts
  const total = Math.max(1, Number(totalBytes) || 0)
  const steps = (Array.isArray(ladder) ? ladder : [])
    .map(Number)
    .filter((s) => Number.isFinite(s) && s >= minBytes && s <= maxBytes)
    .sort((a, b) => a - b)
  for (const seg of steps) {
    if (Math.ceil(total / seg) <= targetBlocks) return seg
  }
  // 阶梯里没有一档够用（文件特别大）⇒ 用允许的最大块，把块数压到最低。
  return Number(maxBytes)
}

/**
 * 从 `/api/status` 的响应里取一键算梯形参（拿不到就退回本地默认）。
 *
 * @param {object|null|undefined} status `/api/status` 的 `data`。
 */
export function ladderOptsFrom(status) {
  const ladder = Array.isArray(status?.segment_ladder) && status.segment_ladder.length
    ? status.segment_ladder
    : SEGMENT_LADDER
  return {
    ladder,
    targetBlocks: Number(status?.segment_target_blocks) > 0
      ? Number(status.segment_target_blocks)
      : SEGMENT_TARGET_BLOCKS,
    minBytes: Number(status?.segment_bytes_min) > 0 ? Number(status.segment_bytes_min) : 64,
    maxBytes: Number(status?.segment_bytes_max) > 0 ? Number(status.segment_bytes_max) : 1048576,
  }
}
