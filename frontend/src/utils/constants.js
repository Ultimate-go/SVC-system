/**
 * 前端常量。
 *
 * 机器台数**不许写死** —— 一律从 /api/nodes、/api/status 取。
 * 这里只放与后端无关的 UI 常量。
 */

/** 演示账号（口令统一 vds12345），登录页一键填充用。 */
export const DEMO_ACCOUNTS = [
  { username: 'admin', display: '管理员', hint: '可查看审计流水与用户管理；无他人文件的解密权限' },
  { username: 'zhangsan', display: '张三', hint: '普通用户；可上传自有文件，对所有文件具备验证权限、仅对自己所持文件具备解密权限' },
  { username: 'wangwu', display: '王五', hint: '另一普通用户（用于展示「验证不受限、解密受限」语义）' },
]

export const DEMO_PASSWORD = 'vds12345'

/**
 * 一次 `POST /api/query` 最多能带多少个下标 —— 与后端 ``MAX_INDICES`` 对齐。
 *
 * ★★ 为什么必须把它当**公共常量**看：块数**没有**上限（位置预算取消之后），
 *   所以“把一份文件的全部块塞进一次请求”对稍大的文件就会 **422**。
 *   凡是“拿一整份文件去取证据/去验证”的地方（入池、集合验证、区间输入）
 *   都必须按这个数**分片或提前拦住**。
 *
 *   审计 F1：`views/files/list` 的「入池」当时是唯一没做保护的那条路。
 */
export const MAX_QUERY_INDICES = 8192

/**
 * 逐块列表类界面（块分布矩阵 / 块明细表）**先列前多少块**，超过就收起来。
 *
 * ★ 为什么是 16：一屏能舒服看完；而几十上百块一路铺出来，页面会被拉得很长，
 *   真正想看的“这几块在谁手上”反而被淹掉（用户反馈）。
 * ★ 为什么是公共常量：矩阵与块明细表讲的是同一件事，两处阈值/文案各写一份
 *   迟早会不一样 —— 现在只剩这里一个数（文案在 `common/BlockPreviewBar.vue`）。
 */
export const BLOCK_PREVIEW_LIMIT = 16

/** 切法三档。 */
export const SPLIT_MODES = [
  // ★ auto = **按文件大小自动**（后端 `auto_segment_bytes` 的阶梓：
  //   小文件 1 KB/块、大文件逐档上升，块数压在 128 以内）。
  //   它不查顾问 —— 顾问只给建议，采不采纳由用户在建议卡里点（见 files/list 的 applyPlan）。
  { value: 'auto', label: '自动（按大小）' },
  { value: 'by_size', label: '按块大小' },
  { value: 'by_count', label: '按块数' },
]

/** prefer 的两个取值（与后端 /api/plan 一致）。 */
export const PLAN_PREFERS = [
  { value: 'fewest_blocks', label: '块数最少' },
  { value: 'finer_updates', label: '单块更小' },
]
