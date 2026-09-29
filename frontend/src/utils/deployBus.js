/**
 * 「部署配置变了」这件小事的广播。
 *
 * 设备页上有两张卡片会动同一份数据（服务器台数、端口）：
 *   · 台数一改，端口那张卡片的**行数**就该跟着变（每台节点一行）；
 *   · 端口表是按**节点 id** 记的，所以它必须按最新的台数重读一遍。
 *
 * 两张卡片是兄弟，谁也不好直接喊对方；而把设备页拖进来当传话筒
 * （ref + emit + props 三处改动）为这点事不值得。于是用一个事件说出去。
 *
 * ★ 为什么不用 Pinia：这里要传的信息量是零 —— 只有一个"该重读了"的信号，
 *   状态本身仍然在后端（`nodes/deploy.json`）与各自的卡片里。
 */

const EVENT = 'vds:deploy-changed'

/** 说一声：部署配置变了（台数或端口），该重读了。 */
export function emitDeployChanged() {
  window.dispatchEvent(new CustomEvent(EVENT))
}

/**
 * 订阅。返回**取消订阅**的函数 —— 组件在 `onBeforeUnmount` 里一定要调它，
 * 否则反复进出这个页面会留下一堆没人管的监听器。
 */
export function onDeployChanged(fn) {
  window.addEventListener(EVENT, fn)
  return () => window.removeEventListener(EVENT, fn)
}
