/**
 * 会话时效：**闲置 5 分钟**就要求重新登录；**刷新页面不算闲置**。
 *
 * 为什么需要它：登录令牌本来就存在 localStorage，刷新并不会掉线；但
 * 「一次会话该活多久」以前没有实现 —— 令牌一签就是 12 小时，人离开工位
 * 也不会自动退出。这里补上**闲置上限**。
 *
 * 判定与刷新无关：最后活动时刻存在 localStorage（`vds_last_active`）。
 * 刷新只把这一刻往后推，不会重置登录态，所以刷新后仍是登录状态；
 * 只有「真的 5 分钟没有任何动作」才会被请回登录页。
 *
 * 三条约束：
 *   ① 只在浏览器侧计时（后端令牌仍有自己的有效期兜底）；
 *   ② 到点先调一次 `/api/auth/logout`，让服务端把那枚令牌一并作废，再清本地；
 *   ③ 活动监听全部 passive，写 localStorage 做 5 秒节流 ——
 *      不在 mousemove 上做任何 DOM 操作，也不每次事件都写盘。
 */

const LAST_KEY = 'vds_last_active'

/* ---------------------------------------------------------------------------
   「这次登录」的标记 —— 给**每次登录只演一遍**用的
   ---------------------------------------------------------------------------
   「初始化与系统自检」页那条流水线是个开场演示：登录管理员后点一次就够，重复点是噪声。
   所以登录成功时盖一个时间戳，演完再盖另一个；两个相等就说明"这一次登录
   已经演过了"。退出重登会刷新前者，于是又能演一遍。

   ★ 为什么不用 sessionStorage：同一浏览器的**新标签页**是新的会话存储，
     换个标签重新进初始化页会又演一遍；而这个标记要的是"这一次登录"。
--------------------------------------------------------------------------- */
export const LOGIN_MARK_KEY = 'vds_login_mark'
export const INIT_PLAYED_KEY = 'vds_init_played_v1'

/** 登录成功时调一次（见 stores/auth.js 的 login）。 */
export function markLogin() {
  try {
    localStorage.setItem(LOGIN_MARK_KEY, String(Date.now()))
  } catch {
    /* 隐私模式：标记写不进去 ⇒ 当作每次都该演一遍（不影响别的东西） */
  }
}

/** 这一次登录是否已经演过初始化流水线。 */
export function initFlowPlayed() {
  try {
    const mark = localStorage.getItem(LOGIN_MARK_KEY)
    return !!mark && localStorage.getItem(INIT_PLAYED_KEY) === mark
  } catch {
    return false
  }
}

/** 演完盖戳（下一次登录会被重置）。 */
export function markInitFlowPlayed() {
  try {
    localStorage.setItem(INIT_PLAYED_KEY, localStorage.getItem(LOGIN_MARK_KEY) || '')
  } catch {
    /* 同上 */
  }
}

/** 闲置上限（毫秒）。改这一个数就能改掉全站的会话时效。 */
export const IDLE_MS = 5 * 60 * 1000

/** 活动时刻写盘的最小间隔：mousemove 很密，不能次次落盘。 */
const WRITE_EVERY_MS = 5000

/** 看门间隔：不必精确到秒，10 秒一次足够。 */
const WATCH_EVERY_MS = 10 * 1000

let last = 0
let lastWrite = 0
let timer = null

function readStored() {
  try {
    const v = Number(localStorage.getItem(LAST_KEY) || 0)
    return Number.isFinite(v) ? v : 0
  } catch {
    // 隐私模式下 localStorage 会抛异常：没有记录 → 当作"从未活动"。
    return 0
  }
}

/** 记一次活动（节流写盘）。 */
export function touch() {
  const now = Date.now()
  last = now
  if (now - lastWrite > WRITE_EVERY_MS) {
    lastWrite = now
    try {
      localStorage.setItem(LAST_KEY, String(now))
    } catch {
      /* 忽略 */
    }
  }
}

/**
 * 距上次活动过了多久（毫秒）。
 *
 * 内存里的值可能比盘上的新，两边取大者；两边都没有 → `Infinity`
 * （从没活动过，按"已过期"处理）。
 */
export function idleMs() {
  const t = Math.max(last, readStored())
  return t ? Date.now() - t : Infinity
}

/** 是否已经超过闲置上限。 */
export function idleExpired() {
  return idleMs() > IDLE_MS
}

/** 清掉活动时刻（退出登录时调用，免得下一个人进来被当成"还在活动"）。 */
export function forgetActivity() {
  last = 0
  lastWrite = 0
  try {
    localStorage.removeItem(LAST_KEY)
  } catch {
    /* 忽略 */
  }
}

/**
 * 开始看门：超过闲置上限时调用一次 `onExpire`，随后自动停表。
 *
 * @param {() => void} onExpire 到点要做的事（登出 + 回登录页由调用方决定）
 * @returns {() => void} 停表函数
 */
export function startIdleWatch(onExpire) {
  for (const ev of ['mousemove', 'mousedown', 'keydown', 'wheel', 'touchstart', 'focus']) {
    window.addEventListener(ev, touch, { passive: true })
  }
  touch()
  stopIdleWatch()
  let fired = false
  timer = setInterval(() => {
    if (fired) return
    if (idleExpired()) {
      fired = true
      stopIdleWatch()
      onExpire()
    }
  }, WATCH_EVERY_MS)
  return stopIdleWatch
}

/** 停表（本身幂等）。 */
export function stopIdleWatch() {
  if (timer !== null) {
    clearInterval(timer)
    timer = null
  }
}
