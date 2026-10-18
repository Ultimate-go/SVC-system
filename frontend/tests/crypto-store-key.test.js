/**
 * 私钥 store 的 `key` getter 必须**跟着解锁状态变**。
 *
 * 背景（2026-10-18 实测踩到的 bug）：`key` 是 Pinia getter，也就是 computed。
 * 它原来的写法是 `key: () => _sk` —— 只读模块级变量，**没有任何响应式依赖**，
 * 于是 Vue 把第一次求值的结果**永久缓存**。表现：
 *
 *   1. 打开页面时（还没登录）有某个组件读过一次 `crypt.key` → 缓存成 null；
 *   2. 登录成功、私钥已在内存里（状态栏也显示「已解锁」）；
 *   3. 点「解密」→ `crypt.key` 仍是缓存下来的 null → 提示“本机尚未持有私钥”。
 *
 * 现在 getter 读一下 `keyEpoch`（解锁 / 锁定 / 接回时 +1），这个文件把那个顺序
 * 钉死：先读 key（prime 缓存）→ 再解锁 → 必须读得到私钥。
 */

import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createPinia, setActivePinia } from 'pinia'

import { useCryptoStore } from '../src/stores/crypto.js'
import { wrapPrivateKey } from '../src/utils/crypto/keywrap.js'

test('key getter：解锁前读过一次，解锁后必须读得到（不能被永久缓存）', () => {
  setActivePinia(createPinia())
  const store = useCryptoStore()

  // ① 解锁前先读一次 —— 这一步会把旧实现里的 computed 缓存成 null
  assert.equal(store.key, null)

  // ② 模拟登录时的本地解封（迭代数调小，测试只要语义）
  const sk = 0x1234abcdn
  const blob = wrapPrivateKey('pw-for-test', sk, {
    iterations: 1000,
    salt: new Uint8Array(16),
  })
  store.unlock('pw-for-test', blob, 'alice')

  // ③ 旧实现到这里会返回 null
  assert.equal(store.unlocked, true)
  assert.equal(store.key, sk, '解锁之后 crypt.key 必须就是那把私钥')
})

test('key getter：锁定后立刻读不到，重新解锁又读得到', () => {
  setActivePinia(createPinia())
  const store = useCryptoStore()

  const sk = 0x5678n
  const blob = wrapPrivateKey('pw-for-test', sk, {
    iterations: 1000,
    salt: new Uint8Array(16),
  })
  store.unlock('pw-for-test', blob, 'alice')
  assert.equal(store.key, sk)

  store.locked()
  assert.equal(store.key, null, '锁定之后不能再返回旧私钥')
  assert.equal(store.unlocked, false)

  store.unlock('pw-for-test', blob, 'alice')
  assert.equal(store.key, sk)
})
