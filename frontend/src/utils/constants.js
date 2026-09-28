/**
 * 前端常量。
 *
 * 机器台数**不许写死** —— 一律从 /api/nodes、/api/status 取。
 * 这里只放与后端无关的 UI 常量。
 */

/** 演示账号（口令统一 vds12345），登录页一键填充用。 */
export const DEMO_ACCOUNTS = [
  { username: 'admin', display: '管理员', hint: '能看审计与用户管理，但解不开别人的文件' },
  { username: 'zhangsan', display: '张三', hint: '4 份病历本的所有者' },
  { username: 'nurse', display: '内科护士', hint: '能验证别人的文件，但解不开' },
  { username: 'ortho', display: '骨科医生', hint: '同上' },
  { username: 'wangwu', display: '王五', hint: '只有一份会议纪要' },
]

export const DEMO_PASSWORD = 'vds12345'

/** 切法三档。 */
export const SPLIT_MODES = [
  { value: 'auto', label: '自动（推荐）' },
  { value: 'by_size', label: '按块大小' },
  { value: 'by_count', label: '按块数' },
]

/** prefer 的两个取值（与后端 /api/plan 一致）。 */
export const PLAN_PREFERS = [
  { value: 'fewest_blocks', label: '块数最少（上传最快）' },
  { value: 'finer_updates', label: '块更小（改块粒度更细）' },
]
