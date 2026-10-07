/**
 * 证据池。
 *
 * ★ 它为什么必须存在：
 *
 *   一份证据只能证明它自己那个下标集合里的内容。你手里有一份覆盖 {1,3,7}
 *   的证据，想验证 {1,3,5}，没法凭空造出含 5 的证据 —— 必须去把那块取回来。
 *   所以界面上需要一个地方攒这些取回来的证据，再把它们的下标并起来重新聚合。
 *
 *   而在设计 B 下「重新聚合」特别便宜：所有块都在同一条向量里，
 *   所以任意下标子集都能聚成一份证据、一次验证。
 *
 * Pinia 只放两样东西：auth 与 pool。这就是那个 pool。
 */

import { defineStore } from 'pinia'
import api from '../api/client'
import { span } from '../utils/format'

const CACHE_KEY = 'vds_pool'

/** 从 sessionStorage 恢复池子（关掉标签页就没了，这是刻意的）。 */
function loadCache() {
  try {
    const raw = sessionStorage.getItem(CACHE_KEY)
    if (!raw) return { cards: [], selectedIds: [], currentN: null, currentFp: null, fileFps: {} }
    const o = JSON.parse(raw)
    return {
      cards: Array.isArray(o.cards) ? o.cards : [],
      selectedIds: Array.isArray(o.selectedIds) ? o.selectedIds : [],
      currentN: typeof o.currentN === 'number' ? o.currentN : null,
      currentFp: typeof o.currentFp === 'string' ? o.currentFp : null,
      fileFps: o.fileFps && typeof o.fileFps === 'object' ? o.fileFps : {},
    }
  } catch {
    return { cards: [], selectedIds: [], currentN: null, currentFp: null, fileFps: {} }
  }
}

const cached = loadCache()

/** id 要接着已有的往下发，否则恢复后新卡片会和旧卡片撞 id。 */
let seq = cached.cards.reduce((m, c) => Math.max(m, Number(c.id) || 0), 0)

export const usePoolStore = defineStore('pool', {
  state: () => ({
    cards: [...cached.cards],
    selectedIds: [...cached.selectedIds],
    currentN: cached.currentN,
    currentFp: cached.currentFp,
    /**
     * 每份文件**当前**的 δ 指纹：``"owner/file_key" -> fp``（从 ``/api/status`` 的
     * ``delta.files[]`` 来）。
     *
     * ★★ 为什么需要它：δ 现在是**逐文件**的，根本没有“全局指纹”可用。
     *   而“这张卡还有效吗”只能拿**它覆盖的那份文件**的当前指纹去对。
     */
    fileFps: { ...cached.fileFps },
  }),

  getters: {
    selectedCards() {
      return this.cards.filter((c) => this.selectedIds.includes(c.id))
    },

    /**
     * ★ 已作废的卡片 id 集合。
     *
     * 判据是 **δ 指纹**，不是 n：上传让 n 变，而**改块 / 清零 / 追加 / 截断**
     * 只改 C（在合并向量上）**不改全局位置总数 n** —— 所以只比 n 是抓不住改块的。
     *
     * ★★ 而且不能用“一个全局 fp”去比（以前就是这样）：δ 已经是**逐文件**的，
     *   那个 `currentFp` 实际上被 `addCard` 写成了“**最后加的那张卡的**指纹”，
     *   拿它去比别的卡只会得出“除了刚取的那张、其余全作废”。
     *
     *   正确的判据是：**这张卡覆盖的每一份文件，它当时的指纹与现在的比** ——
     *   任一份变了就说明这份文件动过了，这张卡上的证据跟着作废。
     */
    staleIds(state) {
      const out = new Set()
      for (const c of state.cards) {
        const files = c.files || c.result?.files || []
        const cur = state.fileFps || {}
        let decided = false
        for (const f of files) {
          const now = cur[`${f.owner}/${f.file_key}`]
          if (now && f.delta_fp) {
            decided = true
            if (now !== f.delta_fp) {
              out.add(c.id)
              break
            }
          }
        }
        if (decided) continue
        // 兜底：老卡没存 files（或 status 没拉到）时，只能退回长度对比 ——
        // 它抓不住改块，但总比什么都不判强。
        if (typeof state.currentN === 'number' && typeof c.n === 'number') {
          if (c.n !== state.currentN) out.add(c.id)
        }
      }
      return out
    },

    staleCount() {
      return this.staleIds.size
    },

    unionIndices() {
      const set = new Set()
      for (const c of this.selectedCards) for (const i of c.indices) set.add(i)
      return [...set].sort((a, b) => a - b)
    },

    unionScope() {
      const files = new Set()
      const owners = new Set()
      for (const c of this.selectedCards) {
        for (const r of c.result?.refs || []) {
          files.add(`${r.owner}/${r.file_key}`)
          owners.add(r.owner)
        }
      }
      return { files, owners }
    },

    isEmpty: (s) => s.cards.length === 0,

    /**
     * 池子里已有的**下标集合**（拼成字符串便于查找）。
     *
     * ★ 判据必须与 :meth:`addCard` 的去重**完全一致** —— 文件列表页的「已入池」
     *   标记就是拿它判的；两边口径不一致会出现“标着已入池，点下去却又新增了一张卡”。
     */
    indexKeys: (s) => new Set(s.cards.map((c) => c.indices.join(','))),
  },

  actions: {
    addCard({ label, result, src = '' }) {
      const indices = [...(result?.indices || [])]
      // ★ 卡片要把"这份证据来自哪份文件的第几块"一并收下：界面靠它说人话，
      //   而不是把内部坐标（全局位置）摊给用户看。
      //   单文件查询（/api/query）的响应里也有 files，所以两条路都存在。
      const files = Array.isArray(result?.files) ? result.files : null
      const dup = this.cards.find(
        (c) => c.indices.length === indices.length && c.indices.join(',') === indices.join(','),
      )
      if (dup) {
        // 同一集合有卡了就把内容换成新的，不能直接 return（否则旧作废标记会残留）。
        dup.result = result
        dup.files = files
        if (src) dup.src = src
        dup.n = typeof result?.delta_n === 'number' ? result.delta_n : dup.n
        dup.fp = typeof result?.delta_fp === 'string' ? result.delta_fp : dup.fp
        dup.files = files
        dup.ts = Date.now()
        this.syncDelta({ n: dup.n })
        this._persist()
        return dup
      }

      const card = {
        id: ++seq,
        label,
        src,
        indices,
        files,
        result,
        n: typeof result?.delta_n === 'number' ? result.delta_n : null,
        fp: typeof result?.delta_fp === 'string' ? result.delta_fp : null,
        ts: Date.now(),
      }
      this.cards.push(card)
      // ★ 只记 n，**不**写 fp（见 `staleIds` 的注释：拿“刚取的这张的 fp”
      //   当全局指纹，会把除了它以外的卡全判成作废）。
      this.syncDelta({ n: card.n })
      this._persist()
      return card
    },

    syncDelta({ fp = null, n = null, fileFps = null } = {}) {
      let dirty = false
      if (typeof fp === 'string' && fp && this.currentFp !== fp) {
        this.currentFp = fp
        dirty = true
      }
      if (typeof n === 'number' && this.currentN !== n) {
        this.currentN = n
        dirty = true
      }
      if (fileFps && typeof fileFps === 'object' && Object.keys(fileFps).length) {
        this.fileFps = { ...fileFps }
        dirty = true
      }
      if (dirty) this._persist()
    },

    toggle(id) {
      const i = this.selectedIds.indexOf(id)
      if (i >= 0) this.selectedIds.splice(i, 1)
      else this.selectedIds.push(id)
      this._persist()
    },

    selectAll() {
      this.selectedIds = this.cards.map((c) => c.id)
      this._persist()
    },

    clearSelection() {
      this.selectedIds = []
      this._persist()
    },

    removeCard(id) {
      this.selectedIds = this.selectedIds.filter((x) => x !== id)
      this.cards = this.cards.filter((c) => c.id !== id)
      this._persist()
    },

    clear() {
      this.cards = []
      this.selectedIds = []
      try {
        sessionStorage.removeItem(CACHE_KEY)
      } catch {
        /* 忽略 */
      }
    },

    _persist() {
      try {
        sessionStorage.setItem(
          CACHE_KEY,
          JSON.stringify({
            cards: this.cards,
            selectedIds: this.selectedIds,
            currentN: this.currentN,
            currentFp: this.currentFp,
            // ★ 一起存下来：否则刷新页面后判定“卡还有效吗”会先退化成只比 n，
            //   要等一次 /api/status 回来才恢复（那一瞬旧卡看着是好的）。
            fileFps: this.fileFps,
          }),
        )
      } catch {
        /* 存储写满或被禁用，忽略。 */
      }
    },

    /**
     * 核心动作：把勾选卡片的下标并起来，向协调者要一份覆盖并集的新证据。
     *
     * ★ 两条路的语义**完全不同**，界面必须说清（也决定卡片 `src` 怎么写）：
     *
     *   * **同一份文件** → 后端走 `_certs_for` + `AggManyToOne`，即论文 §6.5.2 的
     *     `VC.Agg`：把各节点的凭证聚合起来。**不需要全量值**，毫秒级。
     *   * **跨多份文件** → 后端走 `_prove_merged`：先把各文件的承诺抬进一条合并向量
     *     （`C' = ∏ C_f^{E/E_f}`，一份一次模幂），再用合并集的**全量值**重开一份证据
     *     （O(Σ|f|) 次大指数模幂，秒级）。
     *     跨文件为什么不能也走 `Agg`：学位论文 `Def. 29` 的聚合以**同一个承诺 C** 为前提，
     *     而 `Λ_I` 的支撑集只覆盖本文件的补集，跨文件的交叉项抵消不掉。
     *
     * 两条路的**产物一样**：两个群元素（256 字节），之后照样能再聚合、能分解。
     */
    async aggregateSelected() {
      const indices = this.unionIndices
      if (!indices.length) throw new Error('没有选中任何卡片')
      const cross = this.unionScope
      const { data } = await api.post('/api/query', { indices })
      return this.addCard({
        label: `聚合：${span(indices)}`,
        src: cross.files.size > 1
          ? `跨 ${cross.files.size} 份文件归约而来（取密文 + 合并位置集重算）`
          : `由 ${this.selectedCards.length} 张卡聚合而来`,
        result: data,
      })
    },
  },
})
