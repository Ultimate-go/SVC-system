import api from './client'

export const systemApi = {
  status: () => api.get('/api/status'),
  check: () => api.post('/api/admin/check'),
  audit: (params) => api.get('/api/admin/audit', { params }),
  perfSummary: () => api.get('/api/perf/summary'),
}
