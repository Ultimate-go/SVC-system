<script setup>
/**
 * 总览页。对全体用户开放：普通用户与管理员均可访问。
 *
 * 这一页现在回答两个问题，从上到下：
 *   ① **系统在做什么** —— 明文入库到形成完整性证明的处理链路（六段）；
 *   ② **跑得怎么样** —— 运行性能面板（取数、图表与操作明细，见 PerfPanel）。
 *
 * 原先放在这里的「系统参数 / 存储规模 / 逐文件摘要 / 运行状态 / 密钥模型」
 * 已经搬进 **管理员工作台 → 系统初始化**：那些是部署与初始化层面的量，
 * 与「现在跑得多快」不是一回事，混在一页里两边都讲不清。
 *
 * ★ 2026-10-16：「系统自检」也搬到了 **系统初始化**（那里只留一小块 +
 *   一个弹框，不再占总览一整行）。理由一致：它回答的是「这套系统现在对不对」，
 *   属于初始化/维护那一组；而且它需要管理员心态去看那十条检查项。
 *   于是这一页只剩「在做什么」与「跑得怎么样」两件事，顺着看下去即可。
 */
import { ref, computed, onMounted } from 'vue'
import { systemApi } from '../../../api/system'
import { useAuthStore } from '../../../stores/auth'
import ProcessingFlow from '../../../components/chart/ProcessingFlow.vue'
import PerfPanel from '../../../components/chart/PerfPanel.vue'

const auth = useAuthStore()
// ★ 用 store 里的 getter（``src/stores/auth.js`` 的 ``isAdmin``）；
//   别自己写 ``auth.role`` —— user 挂在 ``auth.user`` 上。
const isAdmin = computed(() => auth.isAdmin)
const loading = ref(true)
const error = ref('')
const status = ref(null)

const clusterHealthy = computed(() => !status.value?.nodes_down?.length && !status.value?.under_replicated)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const s = await systemApi.status()
    status.value = s.data
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  load()
})
</script>

<template>
  <div v-if="loading" class="loading" v-loading="true" />
  <div v-else-if="error" class="panel">
    <p class="text-danger">{{ error }}</p>
    <el-button class="mt-3" @click="load">重试</el-button>
  </div>
  <div v-else-if="status" class="dashboard">

    <!-- ① 系统在做什么：处理链路 -->
    <section class="hero-panel panel mb-3" aria-labelledby="overview-title">
      <div class="overview-head">
        <div>
          <span class="eyebrow mono">SYSTEM OVERVIEW / IAVC-VDSS</span>
          <h1 id="overview-title">全局运行态势</h1>
          <p>从文件进入系统到形成可验证证据的完整数据链路。</p>
        </div>
        <div class="head-side">
          <!-- ★ 只对管理员说：普通用户没有「管理员工作台」这一组菜单，
               把人家指向一个进不去的页面，比不说更糟。
               ★ 放在头部而不是单独占一行 —— 这一块的高度有硬指标（≤ 屏高 2/5）。 -->
          <router-link v-if="isAdmin" class="head-link" to="/init">
            一致性核对（登记表 / 节点视图 / 增量摘要）→ 系统初始化
          </router-link>
          <div class="health-pill" :class="clusterHealthy ? 'is-ok' : 'is-warn'">
            <span class="health-dot" />
            <span>{{ clusterHealthy ? '系统运行正常' : '需要关注' }}</span>
            <small class="mono">{{ status.nodes || 0 }} NODES</small>
          </div>
        </div>
      </div>
      <ProcessingFlow />
    </section>

    <!-- ② 跑得怎么样：运行性能 -->
    <div class="section-heading section-heading-spaced">
      <div><span class="section-kicker mono">RUNTIME PERFORMANCE</span><h2>运行性能</h2></div>
      <span class="section-note mono">LIVE SAMPLING</span>
    </div>
    <PerfPanel />
  </div>
</template>

<style scoped>
.loading {
  min-height: 200px;
}
.hero-panel {
  position: relative;
  overflow-x: hidden;
  /* ★ 「全局运行态势」这一块的高度有硬指标：**不超过屏高的 2/5** ——
     这一页要回答的第二个问题是「跑得怎么样」，上面那块不该把运行性能挤出首屏。
     卡片与标题已按紧凑档排过，正常情况下碰不到这个上限；真碰到了就在这一块里滚，
     不影响下面那块。 */
  max-height: 40vh;
  overflow-y: auto;
  scrollbar-width: thin;
  background: radial-gradient(circle at 88% 8%, color-mix(in srgb, var(--accent) 9%, transparent), transparent 34%), var(--bg-panel);
}
.hero-panel::before {
  content: '';
  position: absolute;
  width: 220px;
  height: 220px;
  right: -100px;
  top: -120px;
  border: 1px solid color-mix(in srgb, var(--accent) 18%, transparent);
  border-radius: 50%;
  box-shadow: 0 0 0 24px color-mix(in srgb, var(--accent) 3%, transparent), 0 0 0 48px color-mix(in srgb, var(--accent) 2%, transparent);
  pointer-events: none;
}
.overview-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 20px; margin-bottom: 12px; position: relative; z-index: 1; }
.eyebrow, .section-kicker { color: var(--accent); font-size: 12px; letter-spacing: .13em; }
.overview-head h1 { color: var(--text-1); font-size: 22px; letter-spacing: -.02em; margin: 4px 0 3px; }
.overview-head p { color: var(--text-2); font-size: 13px; margin: 0; }
/* 头部右侧：指路链接在上、健康胶囊在下，右对齐成一条竖线。 */
.head-side { display: flex; flex-direction: column; align-items: flex-end; gap: 8px; }
.head-link { color: var(--accent); font-size: 12px; text-decoration: none; white-space: nowrap; }
.head-link:hover { text-decoration: underline; }
.health-pill { display: inline-flex; align-items: center; gap: 8px; padding: 8px 11px; border: 1px solid var(--line); border-radius: 999px; color: var(--text-1); font-size: 14px; white-space: nowrap; background: color-mix(in srgb, var(--bg-raised) 78%, transparent); }
.health-pill small { color: var(--text-3); font-size: 13px; margin-left: 3px; }
.health-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 0 4px color-mix(in srgb, var(--ok) 12%, transparent); }
.health-pill.is-warn .health-dot { background: var(--warn); box-shadow: 0 0 0 4px color-mix(in srgb, var(--warn) 12%, transparent); }

.section-heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; margin: 14px 1px 8px; }
.section-heading-spaced { margin-top: 18px; }
.section-heading h2 { color: var(--text-1); font-size: 19px; font-weight: 600; margin: 5px 0 0; }
.section-note { color: var(--text-3); font-size: 13px; letter-spacing: .08em; }
/* ★ 「系统自检」那一节的样式（逐项扫描、进度条、结论框）随组件一起搬到了
   components/security/SelfCheck.vue —— 本页不再有 .diag-* / .check-* / .live-mark。 */

/* 大屏（>= 1500px）：限制宽度，避免单行文本过长。 */
@media (min-width: 1500px) {
  .dashboard { max-width: 1560px; margin-left: auto; margin-right: auto; }
}
@media (max-width: 560px) {
  .overview-head { flex-direction: column; }
  .head-side { align-items: flex-start; }
  .health-pill { align-self: flex-start; }
  .section-heading { align-items: flex-start; }
  .section-note { padding-top: 4px; }
}
</style>
