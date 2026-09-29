import api from './client'

/**
 * 服务器台数（部署规模）。
 *
 * 三件事一条链，与后端 `/api/admin/deploy` 那一族一一对应：
 *
 *  - `get()`      现在跑着几台 / 配置里几台 / 要不要重启；
 *  - `plan(n)`    **只算不动**：改成 n 台会怎样（要摘掉哪几台、要搬多少块、
 *                 有没有搬不动的）。界面在保存**之前**调它，把后果摆在按钮旁边；
 *  - `save(n)`    保存（台数变小 ⇒ 后端**先搬块、再写配置**，重启后生效）；
 *  - `restart(n)` 一键重启（停掉节点/后端/前端，再按新台数起一遍）。
 *
 * ★ 这几个请求都带一个自定义的 `silent` 标记：它们的失败信息是后端写好的
 *   **多句中文**（哪几块要搬、哪几块搬不动、下一步该干什么），需要用一个
 *   对话框完整显示。拦截器那个 toast 会把长文本截掉，所以让它别弹
 *   （见 api/client.js 里那一行 `if (err.config?.silent)`）。
 */
const SILENT = { silent: true }

export const deployApi = {
  get: () => api.get('/api/admin/deploy', SILENT),
  plan: (nodeCount) =>
    api.get('/api/admin/deploy/plan', { params: { node_count: nodeCount }, ...SILENT }),
  save: (nodeCount, confirmShrink = false) =>
    api.put(
      '/api/admin/deploy',
      { node_count: nodeCount, confirm_shrink: confirmShrink },
      SILENT,
    ),
  restart: (nodeCount = null) =>
    api.post('/api/admin/deploy/restart', { node_count: nodeCount }, SILENT),
  /** 不改台数，只把不足的副本补齐（某台被摘掉又加回来之后用）。 */
  redistribute: () => api.post('/api/admin/deploy/redistribute', {}, SILENT),

  // ---------------- 端口（与上面同一套模型：改 → 保存 → 重启生效）

  /** 配置里那一套端口 + 现在跑着的那一套（界面要同时看到这两份）。 */
  ports: () => api.get('/api/admin/ports', SILENT),
  /**
   * **只探不写**：每个端口空不空、有没有填重。
   *
   * 返回里 `probe` 逐个端口给状态（free / ours / occupied），
   * `conflicts` 是"自己撞自己"的那种（两样东西填同一个端口）。
   */
  portsPlan: (body) => api.post('/api/admin/ports/plan', body, SILENT),
  /**
   * 保存端口（重启后生效）。
   *
   * ★ 两条分寸不同：**填重了**后端会 409 拦下（一定起不来）；
   *   **被别的程序占着**只回一条 warning，照样保存 —— 那可能是使用者
   *   故意先把配置存下来、过后再腾端口。
   */
  portsSave: (body) => api.put('/api/admin/ports', body, SILENT),
}
