/**
 * 前端表单校验 —— 与后端 schemas.py 保持同一套约束。
 * （后端只认这些，前端要做同样的约束，否则提交上去才被 422 拒掉。）
 *
 * ★ 相对 import 带 `.js`：本模块被 `tests/validate-msg.test.js` 直接喂给
 *   `node --test`，而 Node 的 ESM 解析**不认**无扩展名的相对路径
 *   （Vite 认，所以打包一直是好的）。为了别让一句打包能过、测试跑不起来的
 *   import 挡路，这里写全 —— `crypto/verify.js` 等被测试用到的模块同样是全的。
 */

import { MAX_QUERY_INDICES } from './constants.js'

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
 * 解析**全局位置**输入，支持 `1-4, 9` 这种区间语法。
 * 返回**排序去重的内部下标（0 起）**；语法错抛 Error。
 *
 * ★ 坐标口径（2026-10-12 定）：**用户填的是从 1 开始的编号**，本函数负责 −1
 *   换回内部值；后端与存储一律 0 起（见 `utils/format.js` 的口径说明）。
 *   因此这里也**不接受 0**：填 0 会明确报错，而不是静默当成第 0 个。
 * :param maxCount: 一次最多允许展开成多少个下标。**必须在展开之前判** ——
 *   见函数里的注解（安全审计 I6）。默认值与后端的 ``MAX_INDICES`` 对齐。
 * :param noun: 报错文案里怎么称呼这些数 —— 主路径填的是「第几块」，
 *   所以 ``parseBlockRange`` 传 ``块``；「高级」里填全局编号，就用默认的。
 */
export function parseIndexRange(text, maxCount = MAX_QUERY_INDICES, noun = '下标') {
  const s = String(text ?? '').trim()
  if (!s) return []
  const out = new Set()
  for (const part of s.split(',')) {
    const p = part.trim()
    if (!p) continue
    if (/^\d+$/.test(p)) {
      const n = Number(p)
      if (n < 1) throw new Error(`${noun}从 1 开始，没有 0 号（你填的是 ${p}）`)
      out.add(n - 1)
    } else if (/^(\d+)\s*-\s*(\d+)$/.test(p)) {
      const m = p.match(/^(\d+)\s*-\s*(\d+)$/)
      const a = Number(m[1])
      const b = Number(m[2])
      if (a < 1) throw new Error(`${noun}从 1 开始，没有 0 号（你填的是 ${p}）`)
      if (a > b) throw new Error(`区间下界大于上界：${p}`)
      // ★★ 先判规模、再展开（安全审计 I6）：以前是“先 for 展开、之后才校验”，
      //   于是填一个 1-99999999 就能把页面卡死（内存与 CPU 双吃）。
      //   现在**没等展开**就把它挡回去，而且把上限说清楚。
      if (b - a + 1 > maxCount) {
        throw new Error(
          `区间超出单次上限：${p} 将展开为 ${b - a + 1} 个${noun}，单次上限 ${maxCount} 个`,
        )
      }
      for (let i = a; i <= b; i++) out.add(i - 1)
      if (out.size > maxCount) {
        throw new Error(`一次最多 ${maxCount} 个${noun}（可分几次填），现在已经 ${out.size} 个`)
      }
    } else {
      throw new Error(`格式不合法：${p}（应为从 1 开始的整数或 \`a-b\` 区间，如 1-4, 9）`)
    }
  }
  return [...out].sort((a, b) => a - b)
}

/**
 * 解析「第几块」输入（界面坐标，**从 1 开始**），支持 `1-4, 9` 这种区间语法。
 *
 * ★ 为什么要单开一个名字：界面说的是"第几块"，内部算的是**全局位置**。
 *   这两套坐标必须分开 —— 用户填"第 1-4 块"，前端拿这份文件的
 *   ``block_indices`` 把它换成全局下标再送 /api/query；而不是让用户
 *   去填一个他根本不该看到的全局下标。
 *
 * :param maxBlocks: 这份文件的块数。给了就顺手校验越界 —— 块号从 1 数到
 *   n，越界当场说清"这份文件只有几块"，不必等服务端回一个 400 让人猜。
 *   ★★ 它还要跟 ``MAX_QUERY_INDICES`` **取小**（审计 N2）：块数**没有上限**
 *   （64 字节一块的话，1 MB 文件就能切出 16384 块），只拿块数当上限会
 *   放行一个 >10000 块的区间，然后由 ``/api/query/files`` 回 400。
 */
export function parseBlockRange(text, maxBlocks = null) {
  // 块号是**这份文件内部**的坐标：给了块数就用它当上限（更严）；
  // 没给就退回与后端一致的整体上限。两者取小 —— 见函数头的审计 N2 说明。
  const fileLimit =
    typeof maxBlocks === 'number' && maxBlocks > 0 ? maxBlocks : MAX_QUERY_INDICES
  const limit = Math.min(fileLimit, MAX_QUERY_INDICES)
  // ★ 说「块」而不是「下标」：这一路上用户填的就是块号（见函数头注释）。
  const blocks = parseIndexRange(text, limit, '块')
  if (typeof maxBlocks === 'number' && maxBlocks > 0) {
    // 报错里的数字也用**给人看的编号**（越界的内部值 +1 回显）
    const over = blocks.filter((b) => b >= maxBlocks).map((b) => b + 1)
    if (over.length) {
      throw new Error(
        `该文件共 ${maxBlocks} 块（第 1 至 ${maxBlocks} 块），超出部分：` + `${over.join(', ')}`,
      )
    }
  }
  return blocks
}
