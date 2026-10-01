import api from './client'

export const systemApi = {
  status: () => api.get('/api/status'),
  check: () => api.post('/api/admin/check'),
  /**
   * 审计流水。params 支持：
   *   limit / target / actor —— 原有
   *   action                 —— 动作名精确匹配
   *   ok                     —— true 只看成功 / false 只看被拒
   *   since / until          —— **本地墙上时间**（后端换 UTC），半开区间 [since, until)
   *   q                      —— 模糊匹配（actor/action/target/detail/remark）
   */
  audit: (params) => api.get('/api/admin/audit', { params }),
  /** 库里实际出现过的动作名（给筛选下拉用），按出现次数降序。 */
  auditActions: () => api.get('/api/admin/audit/actions'),
  /** 给一条审计流水写/改/清空人工备注（传空串即清空）。 */
  setAuditRemark: (id, remark) =>
    api.patch(`/api/admin/audit/${id}/remark`, { remark }),
  perfSummary: () => api.get('/api/perf/summary'),
}
