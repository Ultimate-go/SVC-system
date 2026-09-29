<script setup>
/**
 * 服务器台数（管理员）。
 *
 * 它把「改台数」这件事收成**一个入口、一条链路**：
 *
 *     在界面上改 → 保存 → 提示重启 → 点「立刻重启」→ 按新台数起节点和后端
 *
 * 为什么是「重启后生效」而不是热扩容：节点名单在后端启动那一刻就定死了
 * （`Settings.node_ids` 进 `VectorStore`，分片轮转、启动闸、副本表全按它算），
 * 热改它等于在飞行中换引擎。演示规模下重启只要十几秒，而且**数据一行都不动** ——
 * 所以这条取舍很清楚：拿十几秒换掉一整类"半生效状态"的故障。
 *
 * 三条必须让使用者在**点之前**就看见的事（都由 `/deploy/plan` 先算好）：
 *
 *  1. 台数变小 → 会被摘掉哪几台；
 *  2. 台数变小 → 有多少块要**重新分配**（会趁被摘的机器还活着时搬走，不会丢）；
 *  3. 如果某些块**每一份副本都联系不上** → 那是唯一一条真会丢数据的路，
 *     必须单独说清楚，并且要一次显式确认。
 */
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { deployApi } from '../../api/deploy'
import { useAuthStore } from '../../stores/auth'
import { emitDeployChanged } from '../../utils/deployBus'
import Icon from '../icons/Icon.vue'

const info = ref(null)
const plan = ref(null)
const want = ref(4)
const loading = ref(false)
const saving = ref(false)
const restarting = ref(false)
const polling = ref(false)
const error = ref('')

let pollTimer = null

/** 重启会让后端换一把 JWT 签名密钥（启动时随机生成）—— 所以“重启完成”
 *  这件事通常同时意味着“这次会话结束了”。这里要用它来清会话。 */
const auth = useAuthStore()

const dirty = computed(() => !!info.value && want.value !== info.value.node_count)
const running = computed(() => info.value?.running_count ?? 0)

/** 台数变小了没有（相对于**跑着的**台数，而不是配置里的）。 */
const shrinking = computed(() => want.value < running.value)
const growing = computed(() => want.value > running.value)

const lostCount = computed(() => plan.value?.lost?.length || 0)
const orphanCount = computed(() => plan.value?.orphan?.length || 0)

function fmtList(arr, max = 12) {
  const a = arr || []
  const head = a.slice(0, max).join('、')
  return a.length > max ? `${head} …（共 ${a.length} 项）` : head
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await deployApi.get()
    info.value = data
    want.value = data.node_count
    await refreshPlan()
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

/** 只算不动：把"改成 N 台会怎样"先拿回来摆在界面上。 */
async function refreshPlan() {
  plan.value = null
  if (!info.value) return
  if (want.value === info.value.running_count) return
  try {
    const { data } = await deployApi.plan(want.value)
    plan.value = data
    if (data.clamped) {
      ElMessage.warning(`台数被夹到 ${data.node_count}（允许 ${info.value.min}–${info.value.max}）`)
      want.value = data.node_count
    }
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '预估失败'
  }
}

async function save() {
  if (!dirty.value) return
  let confirmShrink = false

  // ★ 两道确认都在**发请求之前**：后端也会拦一次（409），但那是兜底；
  //   能提前拿到计划的时候，就该让使用者看着具体数字决定。
  if (plan.value && lostCount.value) {
    try {
      await ElMessageBox.confirm(
        `有 ${lostCount.value} 块搬不走：下标 ${fmtList(plan.value.lost)}。\n\n` +
          '它们的每一份副本都在联系不上的机器上，现在收缩就把这几块永久丢掉。\n\n' +
          '保险的做法是先把那几台起起来再收缩，那时就搬得动了。\n\n' +
          '要带着这个损失继续吗？',
        '会丢数据',
        {
          type: 'error',
          confirmButtonText: '我知道这几块会丢，继续',
          cancelButtonText: '先起来那几台再说',
          confirmButtonClass: 'el-button--danger',
        },
      )
      confirmShrink = true
    } catch {
      return
    }
  } else if (plan.value && orphanCount.value) {
    try {
      await ElMessageBox.confirm(
        `从 ${running.value} 台减到 ${want.value} 台，要搬走 ${orphanCount.value} 块` +
          `（下标 ${fmtList(plan.value.orphan)}）。\n\n` +
          `这些块现在只在 ${(plan.value.will_be_removed || []).join('、')} 上还有副本。` +
          '保存时会趁那几台还在，把内容搬到留下的机器上；\n' +
          '块内容、下标和承诺都不变，不会丢。\n\n继续吗？',
        '要重新分配文件块',
        { type: 'warning', confirmButtonText: '继续', cancelButtonText: '再想想' },
      )
      confirmShrink = true
    } catch {
      return
    }
  }

  saving.value = true
  try {
    const { data } = await deployApi.save(want.value, confirmShrink)
    ElMessage.success(data.message?.split('\n')[0] || '已保存')
    await load()
    // ★ 台数一变，端口那张卡片的**行数**就得跟着变（每台节点一行），
    //   而它们俩是兄弟卡片 —— 用事件说一声（见 utils/deployBus.js）。
    emitDeployChanged()
  } catch (e) {
    const detail = e?.response?.data?.detail
    if (e?.response?.status === 409 && detail) {
      // 后端拦下来了（我们离计划那一刻之间状态变了）—— 把它的原话完整显示出来。
      ElMessageBox.alert(detail, '这一步被拦下了', { type: 'warning' })
      await load()
    } else {
      error.value = detail || e?.message || '保存失败'
    }
  } finally {
    saving.value = false
  }
}

async function askRestart() {
  const target = info.value?.node_count ?? running.value
  const changed = target !== running.value
  try {
    await ElMessageBox.confirm(
      changed
        ? `重启后按 ${target} 台跑（现在是 ${running.value} 台）。\n\n` +
            '节点、后端、前端会全部停掉再起一遍，大约十几秒。' +
            '期间页面连不上，恢复后自动回到登录页（要重新登录一次）。\n' +
            '数据、账号、文件都不动。'
        : `台数没变（还是 ${running.value} 台），只是停掉重起一遍。\n\n` +
            '大约十几秒，期间页面连不上，恢复后自动回到登录页（要重新登录一次）。',
      '立刻重启',
      { type: 'warning', confirmButtonText: '现在重启', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  restarting.value = true
  try {
    const { data } = await deployApi.restart(null)
    ElMessage.success(`正在重启：${data.from} 台 → ${data.to} 台`)
    startPolling()
  } catch (e) {
    restarting.value = false
    error.value = e?.response?.data?.detail || e?.message || '重启失败'
  }
}

/**
 * 等后端回来。用 `fetch` 而不是项目里那个 axios 实例 —— 拦截器会给每次失败
 * 弹一个 toast，而这里注定要失败好几次（后端正在重启），那会刷一屏红字。
 */
function startPolling() {
  if (polling.value) return
  polling.value = true
  const deadline = Date.now() + 180000
  const tick = async () => {
    if (Date.now() > deadline) {
      polling.value = false
      restarting.value = false
      error.value = '等了两分钟还没起来。看一眼新开的那个重启窗口，里面会说是哪一步卡住了。'
      return
    }
    try {
      // ★ 200 或 401 都算「后端回来了」，而 401 还是**正常**的那一种：
      //   后端每次启动都会随机生成一把 JWT 签名密钥，所以重启之后旧令牌必然失效。
      //   只要它还答话（哪怕是 401），就说明它已经起来了。
      //   原来只认 r.ok，于是重启后永远等不到 200 —— 页面就一直转、只能人去手动开。
      const r = await fetch('/api/status', { cache: 'no-store' })
      if (r.ok || r.status === 401) {
        // 先看会话还在不在：在就刷一下；不在就直接落登录页。
        let alive = false
        try {
          const me = await fetch('/api/auth/me', {
            cache: 'no-store',
            headers: auth.token ? { Authorization: `Bearer ${auth.token}` } : {},
          })
          alive = me.ok
        } catch {
          /* 当作会话已失效 */
        }
        if (alive) {
          ElMessage.success('重启完成，正在刷新页面…')
          setTimeout(() => window.location.reload(), 600)
        } else {
          // ★ 顺序不能反：先清会话再跳。守卫对“已登录访问 /login”是**送回首页**的
          //   —— 令牌还留着就会被弹回 /，根本看不到登录页。
          auth.clear()
          ElMessage.success('重启完成，请重新登录')
          setTimeout(() => window.location.replace('/login'), 600)
        }
        return
      }
    } catch {
      /* 还没起来，正常 */
    }
    pollTimer = setTimeout(tick, 1500)
  }
  // 先等一会儿：重启器自己也要先睡 2 秒（把这次的响应送回去），
  // 立刻去探只会探到"还活着"的旧后端，然后误判成"已经好了"。
  pollTimer = setTimeout(tick, 4000)
}

async function fixReplicas() {
  saving.value = true
  try {
    const { data } = await deployApi.redistribute()
    ElMessage.success(data.message || '已处理')
    await load()
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '操作失败'
  } finally {
    saving.value = false
  }
}

onMounted(load)
onBeforeUnmount(() => {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
})

defineExpose({ refresh: load })
</script>

<template>
  <div class="panel mb-3" v-loading="loading">
    <div class="deploy-head">
      <h4 class="sec-title" style="margin-bottom: 0">服务器台数</h4>
      <span class="mono text-2" style="font-size: 12px">
        跑着 {{ running }} 台<template v-if="info"> · 配置 {{ info.node_count }} 台</template>
      </span>
    </div>

    <p class="note">
      这里决定集群用几台存储服务器，<b>重启后生效</b>。保存之后点「立刻重启」，不用再去按 cmd。
      台数<b>调小</b>时，保存那一步会趁被摘掉的机器还在，把已经承诺过的文件块搬到留下的
      机器上，一块都不会丢。
    </p>

    <div class="deploy-row">
      <span class="deploy-label">台数</span>
      <el-input-number
        v-model="want"
        :min="info?.min ?? 1"
        :max="info?.max ?? 32"
        :disabled="loading || saving || restarting"
        @change="refreshPlan"
      />
      <el-button type="primary" :disabled="!dirty || restarting" :loading="saving" @click="save">
        保存
      </el-button>
      <el-button type="warning" :disabled="loading || saving" :loading="restarting" @click="askRestart">
        <Icon name="refresh" :size="14" style="margin-right: 6px" />立刻重启
      </el-button>
      <el-button text :disabled="loading || saving || restarting" @click="load">刷新</el-button>
    </div>

    <div class="deploy-stats mono">
      <span>运行中 <b>{{ running }}</b> 台<template v-if="info">（{{ (info.running_node_ids || []).join('、') }}）</template></span>
      <span>每块存 <b>{{ info?.replica_factor ?? '—' }}</b> 份副本</span>
      <span>全局块数 <b>{{ info?.blocks ?? '—' }}</b></span>
      <span v-if="info?.fresh_nodes?.length">
        新加入（还是空的）：<b>{{ info.fresh_nodes.join('、') }}</b>
      </span>
    </div>

    <el-alert
      v-if="error"
      type="error"
      :closable="false"
      class="mt-3"
      :title="error"
    />

    <el-alert
      v-if="info?.notice"
      :type="info.restart_required ? 'warning' : 'info'"
      :closable="false"
      class="mt-3"
      title="部署提示"
      :description="info.notice"
    />

    <el-alert
      v-if="info?.restart_required"
      type="warning"
      :closable="false"
      class="mt-3"
      title="改了台数，还没重启"
        :description="`配置里是 ${info.node_count} 台，现在跑的还是 ${running} 台，重启后生效。`"
    />

    <!-- ---------- 收缩前的预估（只算不动，全部来自 /deploy/plan） ---------- -->
    <div v-if="plan && plan.clamped" class="mt-3 text-3" style="font-size: 12px">
      台数被夹到 {{ plan.node_count }}（允许 {{ info?.min }}–{{ info?.max }}）。
    </div>

    <template v-if="plan && shrinking">
      <el-alert
        v-if="lostCount"
        type="error"
        :closable="false"
        class="mt-3"
        title="有块搬不走，现在收缩会永久丢掉"
        :description="`${lostCount} 块：下标 ${fmtList(plan.lost)}。副本都在联系不上的机器上，先把那几台起起来再收缩。`"
      />
      <el-alert
        v-else-if="orphanCount"
        type="warning"
        :closable="false"
        class="mt-3"
        title="要重新分配文件块（不会丢）"
        :description="`${orphanCount} 块要从 ${(plan.will_be_removed || []).join('、')} 搬到 ${plan.keep.join('、')}。保存时趁那几台还在就搬走，块内容、下标和承诺都不变。`"
      />
      <div class="deploy-preview mono mt-3">
        <span>要摘掉：<b class="text-danger">{{ (plan.will_be_removed || []).join('、') || '—' }}</b></span>
        <span>留下来：<b>{{ plan.keep.join('、') }}</b></span>
        <span>要搬的块：<b :class="lostCount ? 'text-danger' : 'text-warn'">{{ orphanCount }}</b></span>
        <span>搬不动的块：<b :class="lostCount ? 'text-danger' : 'text-ok'">{{ lostCount }}</b></span>
      </div>
      <!-- ★ 预判一个一定会被问的问题：下面那张节点表格里，要摘掉的那几台
           **仍然显示持有块** —— 因为搬块是"再存一份"，不是"搬走"；
           而 VDS 只能删向量末尾，那几份多出来的副本结构上删不掉。
           它们已不参与写给/读（账目以留下的那几台为准），重启后就不见了。 -->
      <p v-if="info?.restart_required" class="note mt-3" style="margin-bottom: 0">
        重启前：要摘掉的那几台<b>仍然显示持有块</b>，因为搬块是「再存一份」，不是「搬走」；
        而 VDS 只能删向量末尾，多出来的这几份副本删不掉。它们已经不参与读写，
        <b>重启后这一栏就没了</b>。自检也会把它列成「提示」。
      </p>
    </template>

    <div v-else-if="plan && growing" class="mt-3 text-3" style="font-size: 12px">
      要新加 {{ (plan.will_be_added || []).join('、') }}。
      <b>旧块不动</b>，还在原来那几台上；只有之后新写入的块才会摊到新机器上。
    </div>

    <div class="deploy-foot mt-3">
      <span class="text-3" style="font-size: 12px">
        台数存在 <span class="mono">{{ info?.source || 'nodes/deploy.json' }}</span>；
        「重置并启动.cmd」会把它复位成默认值。
      </span>
      <el-button
        v-if="info && (info.blocks || 0) > 0"
        text
        size="small"
        :disabled="saving || restarting"
        @click="fixReplicas"
      >
        补齐不足的副本
      </el-button>
    </div>
  </div>
</template>

<style scoped>
/* ★ 本组件也要自己定一份 ``.sec-title``：项目的 ``scoped`` 样式不会穿到子组件里，
   而这张卡片用的是 ``class="sec-title"``（与各视图标题同一套写法）。
   定义与 ``views/admin/dashboard`` 里那份保持一致。 */
.sec-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 14px;
}
.deploy-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}
.deploy-row {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.deploy-label {
  font-size: 13px;
  color: var(--text-2);
}
.deploy-stats {
  display: flex;
  gap: 20px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--text-2);
  margin-top: 12px;
}
.deploy-stats b {
  color: var(--text-1);
}
.deploy-preview {
  display: flex;
  gap: 20px;
  flex-wrap: wrap;
  font-size: 12px;
  color: var(--text-2);
}
.deploy-preview b {
  color: var(--text-1);
}
.deploy-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.note {
  font-size: 12px;
  color: var(--text-3);
  margin-bottom: 12px;
  line-height: 1.7;
}
.note b {
  color: var(--text-1);
}
</style>
