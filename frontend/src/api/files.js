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
}
