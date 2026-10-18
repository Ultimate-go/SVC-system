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
  /**
   * **合并**两份**已有**证据（``VC.Agg``）—— 「增量聚合」里的那一步。
   *
   * :param a, b: 各是 ``/api/query`` 响应里那三项（``indices`` / ``values`` /
   *   ``proof``）—— 把聚合面板里的卡片**原样传回来**即可。
   *
   * ★ 它与 ``evidenceApi.query`` 的区别不在结果（两者**逐位相同**），
   *   而在**要不要向节点取数据**：
   *     * ``query``   —— 现在去各节点收凭证、顺手聚起来（有网络往返）；
   *     * ``agg``     —— 凭证早就有了，只把两份**已有**的证据并起来
   *                     （不取密文、不重算分量、参与节点 0 台）。
   *   后者才是"攒着建"的依据：先聚合一部分，以后把新来的并进来即可，
   *   代价只与新并进来的那部分有关。
   *
   * 前提：两份证据在**同一条向量**上（本方案里 = 同一份文件），且互不重叠。
   */
  agg: (a, b) => api.post('/api/evidence/agg', { a, b }),
}
