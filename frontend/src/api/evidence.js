import api from './client'

export const evidenceApi = {
  query: (indices, allowPartial = false) => api.post('/api/query', { indices, allow_partial: allowPartial }),
  /**
   * 按**文件 + 第几块**查询 —— 界面上的主交互走这条。
   *
   * :param targets: ``[[owner, file_key], ...]`` —— 是**数组对**，不是对象。
   *   后端 schema 写的是 ``list[tuple[str, str]]``，发成 ``{owner, file_key}``
   *   会 422（而 422 只笼统地说字段不合法，很难看出是形状问题）。
   * :param blockIndices: 块序号（0 起算）。不传 = 每个文件整份取。
   */
  queryFiles: (targets, blockIndices = null) => api.post('/api/query/files', { targets, block_indices: blockIndices }),
  verifyBatch: (items, locate = true, compare = true) =>
    api.post('/api/query/verify-batch', { items, locate, compare }),
  disagg: (body) => api.post('/api/evidence/disagg', body),
  registry: (globalIndex) => api.get(`/api/registry/${globalIndex}`),
}
