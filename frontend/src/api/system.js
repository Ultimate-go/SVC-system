import api from './client'

export const systemApi = {
  status: () => api.get('/api/status'),
  check: () => api.post('/api/admin/check'),
  /**
   * 审计流水。params 支持：
   *   page / page_size / target / actor —— 已有参数
   *   action                 —— 动作名精确匹配
   *   ok                     —— true 仅成功，false 仅被拒
   *   since / until          —— 本地时间（后端换算为 UTC），半开区间 [since, until)
   *   q                      —— 模糊匹配（actor/action/target/detail/remark）
   */
  audit: (params) => api.get('/api/admin/audit', { params }),
  /** 库中出现过的动作名（用于筛选下拉），按出现次数降序。 */
  auditActions: () => api.get('/api/admin/audit/actions'),
  /** 为一条审计流水写入 / 修改 / 清空人工备注；传空串即清空。 */
  setAuditRemark: (id, remark) =>
    api.patch(`/api/admin/audit/${id}/remark`, { remark }),
  perfSummary: () => api.get('/api/perf/summary'),
}
