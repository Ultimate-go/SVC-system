import api from './client'

export const devicesApi = {
  /**
   * 各节点现状。`{ verify: false }` = 不做密码学自检（`proved` 为 `null`）——
   * 只要“谁在线、持有哪些块”时传它；设备页/节点页要看「视图校验通过」就不传。
   */
  nodes: (params) => api.get('/api/nodes', { params }),
  por: (lambdaPos) => api.post('/api/por', { lambda_pos: lambdaPos }),
  plan: (size, prefer) => api.post('/api/plan', { size, prefer }),
  // 故障演练（仅管理员）：模拟节点下线或永久损毁。
  //   ``knock_out`` 与 ``restore`` 均为破坏性操作，会主动制造全网不一致。
  faultStatus: () => api.get('/api/admin/fault-drill'),
  faultDrill: (body) => api.post('/api/admin/fault-drill', body),
}
