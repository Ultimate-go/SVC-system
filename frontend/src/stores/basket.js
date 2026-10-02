/**
 * 块集合（界面上叫「集合」）。
 *
 * ★ 它和证据池（stores/pool.js）是**两件事**，不要混：
 *
 *   - 证据池攒的是**已经取回来的证据** :math:`\pi_I = (S_I, \Lambda_I)` ——
 *     那是一份凭据，带 δ 指纹、会随 n 变化作废，用来做批量验证与分解；
 *   - 集合攒的是**你要验证哪些块**：一串全局下标 + 每块"是谁的第几块"。
 *     它只是一份**清单**，不依赖 δ、也不会因为 n 变了就作废 ——
 *     唯一会失效的情形是那些块**真的被删掉了**（下标 ≥ 当前 n），
 *     界面上如实标出来即可。
 *
 * 为什么值得把清单单独存一份：同一批块常常要反复验（比如每天回来验一次那 12 块），
 * 有了集合就不用每次重新筛一遍文件。这也正是"预存一个集合"的用处。
 *
 * 持久化用 **localStorage**（而不是证据池那样的 sessionStorage）：
 * 用户要的是"存下来"，关掉标签页、重启浏览器都还得在。
 */

import { defineStore } from 'pinia'

const CACHE_KEY = 'vds_basket'

/** 一条集合记录：全局下标 + 它属于谁的第几块（来源未知时为 null）。 */
function makeItem(i, src = {}) {
  return {
    i: Number(i),
    owner: src.owner ?? null,
    file_key: src.file_key ?? null,
    block_idx: typeof src.block_idx === 'number' ? src.block_idx : null,
  }
}

function load() {
  try {
    const raw = localStorage.getItem(CACHE_KEY)
    if (!raw) return { items: [], n: null }
    const o = JSON.parse(raw)
    const items = Array.isArray(o.items)
      ? o.items.filter((x) => x && Number.isInteger(Number(x.i)) && Number(x.i) >= 0).map((x) => makeItem(x.i, x))
      : []
    return { items, n: typeof o.n === 'number' ? o.n : null }
  } catch {
    return { items: [], n: null }
  }
}

const cached = load()

export const useBasketStore = defineStore('basket', {
  state: () => ({
    items: [...cached.items],
    //: 最近一次看到的全局块数 —— 用它判"集合里的块还在不在"（**不**用来判作废）。
    n: cached.n,
  }),

  getters: {
    /** 去重升序的下标 —— 验证时原样交给 ``POST /api/query``。 */
    indices(state) {
      return [...new Set(state.items.map((x) => x.i))].sort((a, b) => a - b)
    },

    size() {
      return this.indices.length
    },

    /** 按文件分组（详情里那一栏：每个下标是谁的第几块）。 */
    groups(state) {
      const map = new Map()
      for (const it of state.items) {
        const name = it.owner && it.file_key ? `${it.owner} / ${it.file_key}` : '来源未知'
        if (!map.has(name)) {
          map.set(name, { name, owner: it.owner, file_key: it.file_key, items: [] })
        }
        map.get(name).items.push({ i: it.i, block_idx: it.block_idx })
      }
      return [...map.values()].map((g) => ({
        ...g,
        items: g.items.sort((a, b) => a.i - b.i),
        indices: g.items.map((x) => x.i).sort((a, b) => a - b),
        blocks: g.items.length,
      }))
    },

    fileCount() {
      return this.groups.length
    },

    /** 来源未知的块数（从"按全局下标"那边收进来的，可能没有归属信息）。 */
    unknownCount(state) {
      return state.items.filter((x) => !x.owner || !x.file_key).length
    },

    /**
     * 已经被删掉的那些下标（≥ 当前块数）。
     *
     * ★ 只有这一种情况会让集合里的块"失效" —— 集合是清单、不是凭据，
     *   所以 n 变了**不影响**其余下标：验证时后端按**当前** δ 现取现证。
     */
    goneIndices(state) {
      if (typeof state.n !== 'number') return []
      return this.indices.filter((i) => i >= state.n)
    },

    /** 某块在不在集合里（文件列表页/筛选结果里那颗"已入集合"标记用它）。 */
    has: (state) => (i) => state.items.some((x) => x.i === i),

    /** 一个文件的块里有多少已经在集合里。 */
    countOf:
      (state) =>
      (indices = []) =>
        indices.filter((i) => state.items.some((x) => x.i === i)).length,
  },

  actions: {
    /** 收一整份文件的多块（``indices`` 是它的全局下标，升序）。 */
    addFile(file) {
      const indices = file?.indices || []
      if (!indices.length) return 0
      return indices.reduce(
        (n, i, k) =>
          n +
          (this._add(makeItem(i, {
            owner: file.owner,
            file_key: file.file_key,
            // 块序号 = 它在 ``indices`` 里的位置（登记表就是按这个顺序连续分配的）
            block_idx: k,
          }))
            ? 1
            : 0),
        0,
      )
    },

    /**
     * 从一次查询的响应里收块（``/api/query`` 的 ``indices`` + ``refs``）。
     *
     * 用它顺手把来源补全：响应里的 ``refs`` 正是"每个下标 → 谁的第几块"，
     * 所以从「按全局下标验证」那边收进来的块也能标出出处，不必再问一次登记表。
     */
    addResult(result) {
      const refs = new Map()
      for (const r of result?.refs || []) refs.set(r.global_index, r)
      let added = 0
      for (const i of result?.indices || []) {
        const r = refs.get(i)
        if (this._add(makeItem(i, r ? { owner: r.owner, file_key: r.file_key, block_idx: r.block_idx } : {}))) {
          added += 1
        }
      }
      this.syncN(result?.delta_n)
      return added
    },

    addIndices(indices = [], src = {}) {
      let added = 0
      for (const i of indices) if (this._add(makeItem(i, src))) added += 1
      return added
    },

    removeIndex(i) {
      this.items = this.items.filter((x) => x.i !== Number(i))
      this._persist()
    },

    removeFile(name) {
      const keep = []
      for (const it of this.items) {
        const n = it.owner && it.file_key ? `${it.owner} / ${it.file_key}` : '来源未知'
        if (n !== name) keep.push(it)
      }
      this.items = keep
      this._persist()
    },

    clear() {
      this.items = []
      this._persist()
    },

    /** 同步"当前有多少块"（用来标出已经被删掉的下标）。 */
    syncN(n) {
      if (typeof n !== 'number' || n < 0 || this.n === n) return
      this.n = n
      this._persist()
    },

    /** 收一块；已经在集合里就只把来源补上（返回是否**新增**）。 */
    _add(item) {
      const found = this.items.find((x) => x.i === item.i)
      if (found) {
        let dirty = false
        if (!found.owner && item.owner) {
          found.owner = item.owner
          found.file_key = item.file_key
          found.block_idx = item.block_idx
          dirty = true
        }
        if (dirty) this._persist()
        return false
      }
      this.items.push(item)
      this.items.sort((a, b) => a.i - b.i)
      this._persist()
      return true
    },

    _persist() {
      try {
        localStorage.setItem(CACHE_KEY, JSON.stringify({ items: this.items, n: this.n }))
      } catch {
        /* 存储被禁用或写满 —— 忽略（集合仍然在内存里可用） */
      }
    },
  },
})
