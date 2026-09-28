import api from './client'

export const evidenceApi = {
  query: (indices, allowPartial = false) => api.post('/api/query', { indices, allow_partial: allowPartial }),
  queryFiles: (targets, blockIndices = null) => api.post('/api/query/files', { targets, block_indices: blockIndices }),
  verifyBatch: (items, locate = true, compare = true) =>
    api.post('/api/query/verify-batch', { items, locate, compare }),
  disagg: (body) => api.post('/api/evidence/disagg', body),
  registry: (globalIndex) => api.get(`/api/registry/${globalIndex}`),
}
