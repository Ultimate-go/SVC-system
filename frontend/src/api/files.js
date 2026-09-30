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
  decrypt: (id, indices) => api.post(`/api/files/${id}/decrypt`, { indices }),
}
