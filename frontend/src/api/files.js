import api from './client'

export const filesApi = {
  list: () => api.get('/api/files'),
  detail: (id) => api.get(`/api/files/${id}`),
  upload: (formData) => api.post('/api/files', formData),
  patch: (id, body) => api.patch(`/api/files/${id}`, body),
  decrypt: (id, indices) => api.post(`/api/files/${id}/decrypt`, { indices }),
}
