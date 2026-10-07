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
 * 解析**内部坐标**（全局位置）输入，支持 0-3, 8 这种区间语法。
 * 返回排序去重的数组；语法错抛 Error（带行号信息）。
 *
 * ★ 注意：这是**内部坐标**。界面上给用户填的应该是"哪份文件的第几块"
 *   （见 :func:`parseBlockRange`）—— 直接要用户填全局下标，等于把内部
 *   坐标漏到界面上。保留它是因为"分解/聚合"那些演示仍在这张卡已有的
 *   下标集合上做切片，那里本来就在内部坐标里。
 * :param maxCount: 一次最多允许展开成多少个下标。**必须在展开之前判** ——
 *   见函数里的注解（安全审计 I6）。默认值与后端的 ``MAX_INDICES`` 对齐。
 * :param noun: 报错文案里怎么称呼这些数 —— 主路径填的是「第几块」，
 *   所以 ``parseBlockRange`` 传 ``块``；「高级」里直接填全局下标，就用默认的。
 *   同一个函数两种说法，免得界面说的是块、报错说的是下标（实测很割裂）。
 */
export function parseIndexRange(text, maxCount = MAX_QUERY_INDICES, noun = '下标') {
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
      // ★★ 先判规模、再展开（安全审计 I6）：以前是“先 for 展开、之后才校验”，
      //   于是填一个 0-99999999 就能把页面卡死（内存与 CPU 双吃）。
      //   现在**没等展开**就把它挡回去，而且把上限说清楚。
      if (b - a + 1 > maxCount) {
        throw new Error(
          `区间太大了：${p} 要展开成 ${b - a + 1} 个${noun}，一次最多 ${maxCount} 个（可分几次填）`,
        )
      }
      for (let i = a; i <= b; i++) out.add(i)
      if (out.size > maxCount) {
        throw new Error(`一次最多 ${maxCount} 个${noun}（可分几次填），现在已经 ${out.size} 个`)
      }
    } else {
      throw new Error(`${noun}写法不对：${p}`)
    }
  }
  return [...out].sort((a, b) => a - b)
}

/**
 * 解析「第几块」输入（界面坐标），支持 0-3, 8 这种区间语法。
 *
 * ★ 为什么要单开一个名字：界面说的是"第几块"，内部算的是**全局位置**。
 *   这两套坐标必须分开 —— 用户填"第 0-3 块"，前端拿这份文件的
 *   ``block_indices`` 把它换成全局下标再送 /api/query；而不是让用户
 *   去填一个他根本不该看到的全局下标。
 *
 * :param maxBlocks: 这份文件的块数。给了就顺手校验越界 —— 块号从 0 数到
 *   n-1，越界当场说清"这份文件只有几块"，不必等服务端回一个 400 让人猜。
 *   ★★ 它还要跟 ``MAX_QUERY_INDICES`` **取小**（审计 N2）：块数**没有上限**
 *   （64 字节一块的话，1 MB 文件就能切出 16384 块），只拿块数当上限会
 *   放行一个 >8192 块的区间，然后由 ``/api/query/files`` 回 400。
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
    const over = blocks.filter((b) => b >= maxBlocks)
    if (over.length) {
      throw new Error(
        `这份文件只有 ${maxBlocks} 块（第 0 到第 ${maxBlocks - 1} 块），` +
          `你填了 ${over.join(', ')}`,
      )
    }
  }
  return blocks
}
