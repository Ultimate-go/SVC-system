<script setup>
/**
 * 总览页。对全体用户开放：普通用户与管理员均可访问。
 *
 * ★ 2026-10-19：「全局运行态势」（六段处理链路 + 点开看的原理知识）已**整体
 *   迁入 管理员工作台 → 初始化与系统自检**（那里现在多了它，删掉了原本的
 *   「算法与目的」）。于是这一页只剩「跑得怎么样」—— 运行性能面板。
 *
 * 原先放在这里的「系统参数 / 存储规模 / 逐文件摘要 / 运行状态 / 密钥模型」
 * 以及「系统自检」，均已搬进 **管理员工作台 → 初始化与系统自检**：
 * 那些是部署与初始化层面的量，与「现在跑得多快」不是一回事。
 */
import PerfPanel from '../../../components/chart/PerfPanel.vue'
</script>

<template>
  <div class="dashboard">
    <div class="section-heading">
      <div><span class="section-kicker mono">RUNTIME PERFORMANCE</span><h2>运行性能</h2></div>
      <span class="section-note mono">LIVE SAMPLING</span>
    </div>
    <PerfPanel />
  </div>
</template>


<style scoped>
/* 这一页只剩运行性能面板；标题组样式与其它页保持一致。 */
.section-heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; margin: 6px 1px 10px; }
.section-heading h2 { color: var(--text-1); font-size: 19px; font-weight: 600; margin: 5px 0 0; }
.section-kicker { color: var(--accent); font-size: 12px; letter-spacing: .13em; }
.section-note { color: var(--text-3); font-size: 13px; letter-spacing: .08em; }

/* 大屏（>= 1500px）：限制宽度，避免单行文本过长。 */
@media (min-width: 1500px) {
  .dashboard { max-width: 1560px; margin-left: auto; margin-right: auto; }
}
@media (max-width: 560px) {
  .section-heading { align-items: flex-start; }
  .section-note { padding-top: 4px; }
}
</style>
