import api from './client'

export const usersApi = {
  list: () => api.get('/api/admin/users'),
  create: (body) => api.post('/api/admin/users', body),
  patch: (id, body) => api.patch(`/api/admin/users/${id}`, body),
  remove: (id) => api.delete(`/api/admin/users/${id}`),
}
