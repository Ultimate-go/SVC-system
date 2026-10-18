/**
 * 「本账号加密参数」一次性提示的**去重记忆**。
 *
 * 记住**这台浏览器**已经看过哪些账号的参数，于是每个账号只弹一次。
 *
 * 为什么不落库
 * ------------
 * 它只是一条**界面提示**的去重记录：不是权限，也不是密钥状态。
 * 放进库里要加列、加接口、加迁移，而换来的只是「换台机器也不弹」——
 * 换机器恰恰是把这个账号的参数再讲一遍的好时机（新设备第一次用）。
 *
 * 键按**账号**分，所以同一台机器上换个账号登录照样会弹；
 * 而登出**不**清它 —— 登出的目的是不让下一个账号看见上一个账号的东西，
 * 这个标记里没有任何属于上一个账号的信息。
 *
 * 读不到 localStorage 时（隐私模式抛异常）按「没看过」处理：
 * 宁可多弹一次，也不要静默把一个说明页吞掉。
 */

const PREFIX = 'vds_acct_intro_v1:'

/**
 * 这个账号在这台浏览器上是否已经看过参数说明。
 *
 * @param {string} username 账号名
 * @returns {boolean} 已看过返回 true；拿不到账号名时也返回 true（不弹空弹窗）
 */
export function accountIntroSeen(username) {
  const u = String(username || '')
  if (!u) return true
  try {
    return localStorage.getItem(PREFIX + u) === '1'
  } catch {
    return false
  }
}

/**
 * 记下「这个账号的参数说明已经显示过」。
 *
 * @param {string} username 账号名
 */
export function markAccountIntroSeen(username) {
  const u = String(username || '')
  if (!u) return
  try {
    localStorage.setItem(PREFIX + u, '1')
  } catch {
    /* 隐私模式下写不进去：下次还会弹一次，可以接受 */
  }
}
