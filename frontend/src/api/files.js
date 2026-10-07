import api from './client'

export const filesApi = {
  list: () => api.get('/api/files'),
  /**
   * 文件详情。
   *
   * :param withElements: 让后端把**每一块的分量**（群元素，十进制字符串）
   *   一并给回来 —— 界面上"详细"模式拿它显示每块的指纹。
   *   ★ 默认不要：1024 块的文件会多出三百多 KB，而"简略"模式根本不显示。
   */
  detail: (id, withElements = false) =>
    api.get(`/api/files/${id}`, withElements ? { params: { elements: 1 } } : undefined),
  upload: (formData) => api.post('/api/files', formData),
  patch: (id, body) => api.patch(`/api/files/${id}`, body),
  /**
   * 把某一**块**的内容换成**等长的全 0 字节**（只有所有者）。
   *
   * ★ 它走的是 ``op = zero``，而 ``zero`` 在服务端就是**改块**（论文的
   *   ``op = mod``）—— 所以：块仍在（下标不变、仍占存储、``n`` 不变）、
   *   完整性照样验证通过，只是内容换了、版本号 +1。
   *   长度用这一块**原来的长度**（不重新切块），所以 ``total_bytes`` 不变。
   *   请求里**不要**传 ``data_b64`` —— 内容由服务端填。
   */
  zero: (id, blockIdx) => api.patch(`/api/files/${id}`, { op: 'zero', block_idx: blockIdx }),
  /**
   * 删掉整份文件（**只有所有者**）。
   *
   * ★ 新架构下它**只删这份文件自己**：每份文件各占自己的位置段（互不重叠、
   *   也永不回收），所以不再有"连带删掉后面上传的文件"这回事，
   *   也不再有"后面压着别人的文件就 409"。响应里的 ``deleted_files``
   *   正常情况下就只有它一个。
   */
  remove: (id) => api.delete(`/api/files/${id}`),
  decrypt: (id, indices) => api.post(`/api/files/${id}/decrypt`, { indices }),
  /**
   * 取**密文**（不解密）—— 浏览器自解密 + 自验证那条路的入口。
   *
   * ★ 与 ``decrypt`` 的分工就是"服务器可不可信"这条分界线：
   *   ``decrypt`` 让**服务端**把内容解开（后端要拿私钥），
   *   ``cipher`` 只把**材料**交出来（密文段 / IV / 块密钥密文 / 证据 / 公开参数），
   *   解封与解密都在浏览器里做（见 ``utils/crypto/index.js::openBlocks``）。
   *
   * :param withPrimes: 是否让后端把整段素数表一并带上（供浏览器自算 U_n）。
   */
  cipher: (id, indices, withPrimes = true) =>
    api.post(`/api/files/${id}/cipher`, { indices }, { params: { with_primes: withPrimes } }),

  /**
   * **回滚演示**：让服务器对某一块交回「旧版本」。
   *
   * 打开后服务器**真的**开始对这块交回旧密文（后续 /cipher 里换掉），
   * 于是客户端自算的分量对不上当前基准，**验证不通过**。
   *
   * ★ 演示顺序：**先** on=true（存下当前这版）→ **再**改块 → **然后**解密。
   * :param id: 文件 id
   * :param blockIdx: 文件内的第几块（0 起算）
   * :param on: true 打开演示，false 恢复正常
   */
  replay: (id, blockIdx, on) =>
    api.post(`/api/files/${id}/replay`, { block_idx: blockIdx, on }),
}
