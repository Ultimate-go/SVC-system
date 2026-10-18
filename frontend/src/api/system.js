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
  /**
   * 性能画像（总览页的「运行性能」面板默认**每 5 秒**轮询一次）。
   *
   * ★ ``silent``：轮询失败由面板自己就地说明（并继续重试），不再让拦截器弹
   *   一个全局红条 —— 后端重启、代理抽一下都会让某一次请求拿不到响应，
   *   而那不是需要用户处理的事（之前就是这里弹出「请求失败：Network Error」）。
   */
  perfSummary: () => api.get('/api/perf/summary', { silent: true }),
}
