import api from './client'

export const devicesApi = {
  nodes: () => api.get('/api/nodes'),
  pending: () => api.get('/api/nodes/pending'),
  retryPush: () => api.post('/api/nodes/retry-push'),
  por: (lambdaPos) => api.post('/api/por', { lambda_pos: lambdaPos }),
  plan: (size, prefer) => api.post('/api/plan', { size, prefer }),
}
