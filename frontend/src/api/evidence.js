import api from './client'

export const evidenceApi = {
  query: (indices, allowPartial = false) => api.post('/api/query', { indices, allow_partial: allowPartial }),
  /**
   * 按文件与块号查询。界面主交互使用此接口。
   *
   * :param targets: ``[[owner, file_key], ...]`` —— 必须是数组对，不是对象。
   *   后端 schema 为 ``list[tuple[str, str]]``；传 ``{owner, file_key}`` 会返回 422
   *   （422 只提示字段不合法，不易定位为形状问题）。
   * :param blockIndices: 块序号（0 起算）。不传则每个文件整份取。
   */
  queryFiles: (targets, blockIndices = null) => api.post('/api/query/files', { targets, block_indices: blockIndices }),
  verifyBatch: (items, locate = true, compare = true) =>
    api.post('/api/query/verify-batch', { items, locate, compare }),
  disagg: (body) => api.post('/api/evidence/disagg', body),
  registry: (globalIndex) => api.get(`/api/registry/${globalIndex}`),
}
