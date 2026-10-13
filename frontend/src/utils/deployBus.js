/**
 * 部署配置变更的广播通道。
 *
 * 存储节点页有两张卡片读写同一份数据（服务器台数、端口）：
 *   · 台数变化后，端口卡片的行数随之变化（每台节点一行）；
 *   · 端口表按节点 id 记录，需按最新台数重新读取。
 *
 * 两张卡片是同级组件，无法直接互相调用；为此引入 ref/emit/props 三层改动不必要，
 * 故用一个事件广播。
 *
 * 不使用 Pinia：本通道不携带状态，只传递「需重新读取」信号；
 * 状态本身仍在后端（`nodes/deploy.json`）与各自的卡片中。
 */

const EVENT = 'vds:deploy-changed'

/** 广播部署配置变更（台数或端口），订阅方需重新读取。 */
export function emitDeployChanged() {
  window.dispatchEvent(new CustomEvent(EVENT))
}

/**
 * 订阅。返回取消订阅函数；组件必须在 `onBeforeUnmount` 中调用，
 * 否则反复进出该页面会累积监听器。
 */
export function onDeployChanged(fn) {
  window.addEventListener(EVENT, fn)
  return () => window.removeEventListener(EVENT, fn)
}
