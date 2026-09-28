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
    if (!raw) return { cards: [], selectedIds: [], currentN: null, currentFp: null }
    const o = JSON.parse(raw)
    return {
      cards: Array.isArray(o.cards) ? o.cards : [],
      selectedIds: Array.isArray(o.selectedIds) ? o.selectedIds : [],
      currentN: typeof o.currentN === 'number' ? o.currentN : null,
      currentFp: typeof o.currentFp === 'string' ? o.currentFp : null,
    }
  } catch {
    return { cards: [], selectedIds: [], currentN: null, currentFp: null }
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
  }),

  getters: {
    selectedCards() {
      return this.cards.filter((c) => this.selectedIds.includes(c.id))
    },

    /**
     * ★ 已作废的卡片 id 集合。判据是 δ 指纹，不是 n。
     * 上传让 n 变、改块改 C 不改 n —— 两者都让旧证据失效，所以按指纹判才自洽。
     */
    staleIds(state) {
      const out = new Set()
      for (const c of state.cards) {
        if (state.currentFp) {
          if (c.fp !== state.currentFp) out.add(c.id)
          continue
        }
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
  },

  actions: {
    addCard({ label, result, src = '' }) {
      const indices = [...(result?.indices || [])]
      const dup = this.cards.find(
        (c) => c.indices.length === indices.length && c.indices.join(',') === indices.join(','),
      )
      if (dup) {
        // 同一集合有卡了就把内容换成新的，不能直接 return（否则旧作废标记会残留）。
        dup.result = result
        if (src) dup.src = src
        dup.n = typeof result?.delta_n === 'number' ? result.delta_n : dup.n
        dup.fp = typeof result?.delta_fp === 'string' ? result.delta_fp : dup.fp
        dup.ts = Date.now()
        this.syncDelta({ fp: dup.fp, n: dup.n })
        this._persist()
        return dup
      }

      const card = {
        id: ++seq,
        label,
        src,
        indices,
        result,
        n: typeof result?.delta_n === 'number' ? result.delta_n : null,
        fp: typeof result?.delta_fp === 'string' ? result.delta_fp : null,
        ts: Date.now(),
      }
      this.cards.push(card)
      this.syncDelta({ fp: card.fp, n: card.n })
      this._persist()
      return card
    },

    syncDelta({ fp = null, n = null } = {}) {
      let dirty = false
      if (typeof fp === 'string' && fp && this.currentFp !== fp) {
        this.currentFp = fp
        dirty = true
      }
      if (typeof n === 'number' && this.currentN !== n) {
        this.currentN = n
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
          }),
        )
      } catch {
        /* 存储写满或被禁用，忽略。 */
      }
    },

    /** 核心动作：把勾选卡片的下标并起来，向协调者要一份覆盖并集的新证据。 */
    async aggregateSelected() {
      const indices = this.unionIndices
      if (!indices.length) throw new Error('没有选中任何卡片')
      const { data } = await api.post('/api/query', { indices })
      return this.addCard({
        label: `聚合：${span(indices)}`,
        src: `由 ${this.selectedCards.length} 张卡聚合而来`,
        result: data,
      })
    },
  },
})
