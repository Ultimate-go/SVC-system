/**
 * 浏览器侧的**密钥状态** —— "私钥留在客户端"这件事的落点。
 *
 * 与后端的关系（这条界线是本文件存在的原因）
 * -----------------------------------------
 * 旧结构：登录时后端用口令把 SM2 私钥解封出来、扣在**后端内存**里
 * （`mgr.remember_key(token, sk)`），前端只是拿一张令牌。于是"服务器不可信"
 * 这句话无从谈起 —— 服务端自己就能解开一切。
 *
 * 现在：后端**只把私钥密文**（`users.sk_wrapped`）交给浏览器，
 * 口令与私钥都不出浏览器。解封发生在这里，`sk` 只活在当前页面的内存中。
 *
 * 三条纪律
 * --------
 * 1. **不落 localStorage**：私钥不进任何**长期**存储。
 *    页面刷新后要能接着用（用户要求：刷新不该把人踢回登录页），
 *    所以存在 **sessionStorage**（同一个标签页、关标签即失效），
 *    并且只接 **5 分钟的闲置窗口** —— 超出窗口就当没登录过，
 *    与全站的会话时效是同一个数（见 utils/session.js）。
 *    —— 存下来就等于把"口令封装私钥"这层保护又还回去了，
 *       所以窗口期与作用域都收到最小。
 * 2. **不进 Pinia state**：state 会被 Vue 响应式代理与 devtools 序列化碰到。
 *    私钥放在模块级变量里，store 只暴露一个 `unlocked` 布尔。
 * 3. **不参与任何请求**：后端永远收不到 `sk`。要用它的地方（解块密钥、
 *    解内容）全部在浏览器里完成，见 `utils/crypto/index.js::openBlocks`。
 */

import { defineStore } from 'pinia'
import { unwrapPrivateKey, unwrapPrivateKeyAsync, wrapPrivateKey } from '../utils/crypto/keywrap.js'

/**
 * 私钥标量。**模块级**，刻意不是 Pinia state。
 * 页面一刷新就回到 `null` —— 这是设计，不是缺陷。
 */
let _sk = null

/**
 * 跨刷新续用私钥的位置：**sessionStorage**（不是 localStorage）。
 *
 * 为什么不是 localStorage：那会让私钥活得跟"这台机器"一样久。
 * sessionStorage 的作用域是**当前标签页**，关掉标签就没了；
 * 再加上 5 分钟的闲置窗口（`SESSION_MAX_MS`），
 * 把"刷新后还能接着用"与"不用就一直不在"两个要求同时满足。
 */
const SESSION_KEY = 'vds_sk_session'
const SESSION_MAX_MS = 5 * 60 * 1000

function saveSession(sk, username) {
  try {
    sessionStorage.setItem(SESSION_KEY, JSON.stringify({ sk: String(sk), username, at: Date.now() }))
  } catch {
    /* 隐私模式下会抛异常：只是刷新后续用不了，不影响别的 */
  }
}

function dropSession() {
  try {
    sessionStorage.removeItem(SESSION_KEY)
  } catch {
    /* 忽略 */
  }
}

/**
 * 私钥的**最长寿命**（毫秒）：到点自动锁掉（安全审计 I9）。
 *
 * ★ 以前解锁之后就一直活着，直到登出 / 401 / 刷新 —— 窗口期等于整个会话。
 *   而“私钥只活在最小心智范围内”是本项目的纪律：既然它只值一份内存，
 *   那就别让它无期限地躺着。
 *
 *   取 15 分钟：一次演示 / 一轮调试不会被打断，而离开工位忘了退出时
 *   也不会让私钥无限期可读。
 */
const KEY_LIFETIME_MS = 15 * 60 * 1000

//: 私钥在浏览器里的**绝对**上限（安全审计 N3）。
//:
//: ★ 为什么是“闲置 + 绝对”**两条**，而不是二选一：
//:   * 只留绝对上限 —— 连续操作超过 15 分钟会被**中途**锁掉，长任务体验断裂；
//:   * 只留闲置续期 —— 一个被遗忘的标签页会**永远**握着私钥。
//:   所以两条都留：每次真正取用私钥（`key`）就把闲置计时重置，
//:   但从**解锁那一刻**算起的绝对上限不延长。
const KEY_ABSOLUTE_MS = 2 * 60 * 60 * 1000

let _expiryTimer = null

function clearExpiry() {
  if (_expiryTimer !== null) {
    clearTimeout(_expiryTimer)
    _expiryTimer = null
  }
}

function armExpiry(store) {
  clearExpiry()
  const left = store.unlockedAt
    ? store.unlockedAt + KEY_ABSOLUTE_MS - Date.now()
    : KEY_LIFETIME_MS
  if (left <= 0) {
    // 已经到绝对上限了：下一拍就锁（**不同步**在调用方里面锁 ——
    // 调用方可能是 `key` 那个 getter，在求值过程中改 state 会踩 Vue 的警告）。
    _expiryTimer = setTimeout(() => {
      if (_sk !== null) {
        store.locked()
        store.error =
          `私钥在浏览器里已达绝对上限（${KEY_ABSOLUTE_MS / 3600000} 小时），` +
          '已自动锁定 —— 重新登录即可'
      }
    }, 0)
  } else {
    _expiryTimer = setTimeout(() => {
      if (_sk !== null) {
        store.locked()
        store.error =
          `私钥在浏览器里闲置超过 ${KEY_LIFETIME_MS / 60000} 分钟，` +
          '已自动锁定 —— 重新登录即可'
      }
    }, Math.min(KEY_LIFETIME_MS, left))
  }
  // ★ Node（测试环境）里这个定时器会阻止进程退出 —— unref 掉它。
  //   浏览器没有 unref，那一句自然跳过。
  if (typeof _expiryTimer?.unref === 'function') _expiryTimer.unref()
}

/** 解封成功后统一落地（同步 / 异步两条路共用，免得两份状态代码漂移）。 */
function applyUnlocked(store, sk, username) {
  _sk = sk
  store.unlocked = true
  store.username = username
  store.unlockedAt = Date.now()
  store.error = ''
  // ★ 私钥换了 ⇒ 让 `key` 那个 getter 失效重算（它靠这个计数器做依赖，见 state 里那段注释）
  store.keyEpoch += 1
  // 刷新后还能接着用（同一标签页、限 5 分钟窗口）
  saveSession(sk, username)
  // 给它一个寿命（安全审计 I9）
  armExpiry(store)
}

/** 解封失败统一收尾（口令不对 / 密文被改，都是同一句话）。 */
function applyUnlockFailure(store, e) {
  store.error = e?.message || '解封私钥失败'
  store.locked()
}

export const useCryptoStore = defineStore('crypto', {
  state: () => ({
    /** 浏览器里现在有没有私钥。 */
    unlocked: false,
    /** 解的是谁的口令（换账号要立刻锁掉，免得拿错私钥去解别人的块）。 */
    username: '',
    /** 解锁时刻（界面显示"已解锁 xx 秒"用）。 */
    unlockedAt: 0,
    /** 最后一次失败的原因（口令错 / 密文被改）。 */
    error: '',
    /**
     * 私钥的**版本号**：只在解锁 / 锁定 / 接回时 +1。
     *
     * ★★ 为什么需要它（2026-10-18 实测踩到的 bug）：`key` 是一个 Pinia getter，
     *   而 Pinia 的 getter 就是 computed。如果它只读模块级变量 `_sk`，那就
     *   **没有任何响应式依赖** —— Vue 会把第一次求值的结果**永久缓存**。
     *   于是“登录前读过一次 `crypt.key`（那时是 null），登录成功之后也永远读到 null”，
     *   表现就是：状态栏写着「已解锁 · 私钥在本浏览器」，点解密却说“本机尚未持有私钥”。
     *   读一下这个计数器，就给 getter 一个真实依赖。
     */
    keyEpoch: 0,
  }),

  getters: {
    /** 私钥本身（调用方用完就该丢掉，别存到别处）。 */
    key: (s) => {
      // ★ 必须读一下响应式字段，否则这个 getter 会被永久缓存（见上面 keyEpoch 那段）。
      void s.keyEpoch
      return _sk
    },
    /** 已解锁时还能免密多久 —— 这里没有超时，纯展示用。 */
    ageMs: (s) => (s.unlockedAt ? Date.now() - s.unlockedAt : 0),
  },

  actions: {
    /**
     * 用口令在浏览器里解出私钥。
     *
     * 登录时口令就在手上，所以**不需要**让用户再输一次 —— 登录动作里顺带调用。
     *
     * @param {string} password 登录口令
     * @param {object} blob 私钥密文（登录响应里的 `key_blob`）
     * @param {string} username 归属（换账号时用来判断要不要锁掉旧的）
     * @throws {Error} 口令不对 / 密文被改（两者在密码学上分不开，同一句话）
     */
    unlock(password, blob, username = '') {
      try {
        applyUnlocked(this, unwrapPrivateKey(password, blob), username)
      } catch (e) {
        applyUnlockFailure(this, e)
        throw e
      }
      return true
    },

    /**
     * 同上，但**分片异步**：解封过程中主线程会定期让出，界面不冻。
     *
     * ★ 登录走这条路（见 `stores/auth.js`）。为什么非异步不可：
     *   PBKDF2 的 20 万轮是纯 JS 同步计算，一口气算完要 2~3 秒；
     *   那几秒里页面**一帧都画不出来** —— 登录页的等待动画会当场僵住
     *   （实测：一次 2784ms 的长任务，正好等于整段解封）。
     *   计算量与结果都与 `unlock` **一模一样**，只是被切成 10ms 一片。
     *
     * @param {string} password 登录口令
     * @param {object} blob 私钥密文（登录响应里的 `key_blob`）
     * @param {string} username 归属（换账号时用来判断要不要锁掉旧的）
     * @param {(fraction: number) => void} [onProgress] 派生 KEK 的真实完成度（0→1）
     */
    async unlockAsync(password, blob, username = '', onProgress = null) {
      try {
        const sk = await unwrapPrivateKeyAsync(password, blob, { onProgress })
        applyUnlocked(this, sk, username)
      } catch (e) {
        applyUnlockFailure(this, e)
        throw e
      }
      return true
    },

    /** 丢掉私钥（退出登录、或换账号时）。 */
    locked() {
      clearExpiry()
      _sk = null
      dropSession()
      this.unlocked = false
      this.username = ''
      this.unlockedAt = 0
      // 私钥没了 ⇒ 同样要让 `key` 那个 getter 失效（否则它还返回旧值）
      this.keyEpoch += 1
    },

    /**
     * 刷新后把上一次解锁的私钥接回来。
     *
     * 三道闸门，任一不过就丢掉这份记录（返回 false）：
     *   ① 只在当前标签页的 sessionStorage 里找；
     *   ② 超过 5 分钟闲置窗口 —— 与全站会话时效同一个窗口；
     *   ③ 账号对不上（别人在这个标签页登过）。
     *
     * @param {string} [username] 当前登录的用户名
     * @returns {boolean} 是否成功接回
     */
    restore(username = '') {
      if (_sk !== null) return false
      let rec = null
      try {
        rec = JSON.parse(sessionStorage.getItem(SESSION_KEY) || 'null')
      } catch {
        rec = null
      }
      if (!rec?.sk) return false
      if (Date.now() - Number(rec.at || 0) > SESSION_MAX_MS) {
        dropSession()
        return false
      }
      if (username && rec.username && rec.username !== username) {
        dropSession()
        return false
      }
      _sk = BigInt(rec.sk)
      this.unlocked = true
      this.username = rec.username || username
      this.unlockedAt = Date.now()
      this.keyEpoch += 1
      armExpiry(this)
      return true
    },

    /**
     * 用到私钥时把**闲置计时**重置一次（安全审计 N3）。
     *
     * ★ 为什么需要它：原来只在“解锁 / 重封”时计时，于是**绝对过期**，
     *   连续操作超过 15 分钟会被中途锁掉（长任务体验断裂）。
     *   现在闲置 15 分钟才锁，但从解锁那刻算起的**绝对上限（2 小时）不延长**
     *   —— 否则一个被遗忘的标签页会永远握着私钥。
     */
    touch() {
      if (this.unlocked && _sk !== null) armExpiry(this)
    },

    /**
     * 用**新口令**在本地把私钥重新封装（安全审计 I8）。
     *
     * 返回可直接交给后端 ``PATCH /api/admin/users/{id}`` 的 ``sk_wrapped``。
     *
     * ★ 私钥本体**不变** —— 改口令只是换外面那层壳，
     *   所以已上传的文件一把都不用重传（这正是“口令封装私钥”相对
     *   “直接用口令派生私钥”的关键好处）。
     *
     * @param {string} newPassword 新口令
     * @returns {object} 私钥密文（dict）
     * @throws {Error} 浏览器里还没有私钥（未解锁）—— 那就先登录一次
     */
    rewrap(newPassword) {
      if (_sk === null) {
        throw new Error('浏览器里还没有私钥 —— 先登录一次解锁，才能在本地重封')
      }
      const blob = wrapPrivateKey(newPassword, _sk)
      // 刚用过它 ⇒ 寿命重新计算（不然重封完下一秒就被自动锁掉了）
      armExpiry(this)
      return blob
    },

    /** 换账号前调用：解的是别人就锁掉，免得拿错私钥。 */
    lockIfOther(username) {
      if (this.unlocked && this.username && username && this.username !== username) {
        this.locked()
      }
    },
  },
})
