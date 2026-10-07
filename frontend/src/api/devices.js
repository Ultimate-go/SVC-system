import api from './client'

export const devicesApi = {
  nodes: () => api.get('/api/nodes'),
  pending: () => api.get('/api/nodes/pending'),
  retryPush: () => api.post('/api/nodes/retry-push'),
  por: (lambdaPos) => api.post('/api/por', { lambda_pos: lambdaPos }),
  plan: (size, prefer) => api.post('/api/plan', { size, prefer }),
  // ★ 故障演练（仅管理员）—— 模拟节点突然下线 / 永久损毁。
  //   ``knock_out`` 与 ``restore`` 都是**破坏性**操作（会主动制造全网不一致）。
  faultStatus: () => api.get('/api/admin/fault-drill'),
  faultDrill: (body) => api.post('/api/admin/fault-drill', body),
}
