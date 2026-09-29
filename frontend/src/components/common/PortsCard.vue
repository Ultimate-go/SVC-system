<script setup>
/**
 * 端口（仅管理员）：后端 / 前端 / 每一台存储节点。
 *
 * 与上面那张「服务器台数」是**同一套模型**：
 *
 *     在界面上改 → 保存 → 提示重启 → 点「立刻重启」→ 按新端口起一遍
 *
 * 为什么不能热改：三个角色的端口都是**启动那一刻绑上去**的
 * （uvicorn 的 `--port`、vite 的 `--port`、节点进程的 `--port`），
 * 进程活着的时候给它换端口等于换门牌 —— 只能重起。演示规模下重启十几秒，
 * 而数据一行都不动（端口与"第几台"无关，块、下标、承诺全都不受影响）。
 *
 * 三条必须让使用者在**点之前**就看见的事：
 *
 *  1. 哪个端口现在被**别的程序占着** —— 只提示、不拦（可以先存下来，
 *     过后再腾端口）；界面上绝不显示"被谁占着"这种别人的进程信息；
 *  2. 哪些端口**填重了** —— 这一条会拦下来：两样东西填同一个端口
 *     （两个节点之间、节点与前后端之间）重启时后起的那个一定起不来；
 *  3. 改了**前端**端口，重启后地址就变了 —— 当前这个页面会失效，
 *     重启器会在新地址自动开一个，这张卡片也会把新地址说清楚。
 */
import { ref, computed, onMounted, onBeforeUnmount } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { deployApi } from '../../api/deploy'
import { useAuthStore } from '../../stores/auth'
import { onDeployChanged } from '../../utils/deployBus'
import Icon from '../icons/Icon.vue'

const info = ref(null)
/** 表单：后端 / 前端 / 每台节点一个端口（节点那份按 **id** 存，与后端一致）。 */
const form = ref({ backend: 0, frontend: 0, nodes: {} })
/** 「检查占用」的结果。**输入一变就作废**（见 invalidate）。 */
const probe = ref([])
const checked = ref(false)
const probing = ref(false)
const loading = ref(false)
const saving = ref(false)
const restarting = ref(false)
const polling = ref(false)
const error = ref('')

/** 重启会让后端换一把 JWT 签名密钥 —— 所以"重启完成"通常同时意味着
 *  "这次会话结束了"。这里用它来清会话，落回登录页。 */
const auth = useAuthStore()

let pollTimer = null
let offBus = null

const nodeIds = computed(() => info.value?.node_ids || [])
const running = computed(() => info.value?.running || {})
const runningNodes = computed(() => running.value.nodes || {})
/** 单进程模式（节点是同一个进程里的对象）—— 那时没有"节点端口"这回事。 */
const singleProcess = computed(() => !!info.value && running.value.distributed === false)

const dirty = computed(() => {
  if (!info.value) return false
  if (form.value.backend !== info.value.backend) return true
  if (form.value.frontend !== info.value.frontend) return true
  return nodeIds.value.some((id) => form.value.nodes[id] !== info.value.nodes[id])
})

/** 填重了的那种（本地就能判，不必等后端；后端也会拦一次）。 */
const localConflicts = computed(() => {
  const seen = new Map()
  const out = []
  const push = (label, port) => {
    if (!port) return
    if (seen.has(port)) out.push(`${label} 与「${seen.get(port)}」填了同一个端口 ${port}`)
    else seen.set(port, label)
  }
  push('后端', form.value.backend)
  push('前端', form.value.frontend)
  nodeIds.value.forEach((id) => push(id, form.value.nodes[id]))
  return out
})

const occupied = computed(() => (probe.value || []).filter((x) => x.status === 'occupied'))

const STATUS_TEXT = { free: '空闲', ours: '本系统在用', occupied: '被别人占用' }
const STATUS_TYPE = { free: 'success', ours: 'info', occupied: 'warning' }

/**
 * 某个端口的探测状态。
 *
 * ★ 输入改过之后（`checked === false`）一律返回 null：拿旧结论给新端口背书
 *   比不给结论更坏 —— 使用者会以为"刚才探过了，没问题"。
 */
function statusOf(scope, id) {
  if (!checked.value) return null
  const row = (probe.value || []).find((x) => x.scope === scope && x.id === id)
  return row ? row.status : null
}

/** 输入变了 → 旧探测结果作废。 */
function invalidate() {
  checked.value = false
  probe.value = []
}

function collect() {
  return {
    backend: form.value.backend,
    frontend: form.value.frontend,
    nodes: { ...form.value.nodes },
  }
}

/** 填默认值（与「重置并启动.cmd」复位成的那一套一致，由后端给过来）。 */
function useDefaults() {
  const d = info.value?.defaults
  if (!d) return
  form.value.backend = d.backend
  form.value.frontend = d.frontend
  const next = {}
  nodeIds.value.forEach((id, i) => {
    next[id] = d.node_base + i
  })
  form.value.nodes = next
  invalidate()
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await deployApi.ports()
    info.value = data
    form.value = { backend: data.backend, frontend: data.frontend, nodes: { ...data.nodes } }
    invalidate()
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function checkPorts() {
  if (localConflicts.value.length) {
    await ElMessageBox.alert(
      localConflicts.value.join('\n') + '\n\n同一个端口只能给一样东西用 —— 先改掉其中一方。',
      '这几个端口填重了',
      { type: 'warning' },
    )
    return
  }
  probing.value = true
  try {
    const { data } = await deployApi.portsPlan(collect())
    probe.value = data.probe || []
    checked.value = true
    if ((data.warnings || []).length) ElMessage.warning('有端口被别的程序占着，见下面的结果')
    else ElMessage.success('这些端口都能用')
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '探测失败'
  } finally {
    probing.value = false
  }
}

async function save() {
  if (!dirty.value) return
  if (localConflicts.value.length) {
    await ElMessageBox.alert(
      localConflicts.value.join('\n') + '\n\n同一个端口只能给一样东西用 —— 先改掉其中一方。',
      '这几个端口填重了',
      { type: 'warning' },
    )
    return
  }

  // ★ 保存前先探一次：被占用的端口要**在保存之前**说清楚。
  //   探测本身失败（比如网络抖了）**不挡保存** —— 占用只是提示，
  //   没理由因为探不到就让使用者存不下配置。
  let warnings = []
  try {
    const { data } = await deployApi.portsPlan(collect())
    probe.value = data.probe || []
    checked.value = true
    warnings = data.warnings || []
    if ((data.conflicts || []).length) {
      await ElMessageBox.alert(data.detail || data.conflicts.join('\n'), '这几个端口填重了', {
        type: 'warning',
      })
      return
    }
  } catch {
    /* 探不到就算了，下面照样保存 */
  }

  if (warnings.length) {
    try {
      await ElMessageBox.confirm(
        warnings.join('\n') + '\n\n配置还是可以存下来：换好端口之后重启一次就生效。',
        '有端口被别的程序占着',
        { type: 'warning', confirmButtonText: '仍然保存', cancelButtonText: '先改一改' },
      )
    } catch {
      return
    }
  }

  saving.value = true
  try {
    const { data } = await deployApi.portsSave(collect())
    ElMessage.success((data.message || '已保存').split('\n')[0])
    const frontendChanged = data.frontend !== running.value.frontend
    await load()
    await askRestart(frontendChanged ? data.frontend : null)
  } catch (e) {
    const detail = e?.response?.data?.detail
    if (e?.response?.status === 409 && detail) {
      // 后端拦下来了（本地版本没算出来的那种）—— 把它的原话完整显示出来。
      await ElMessageBox.alert(detail, '这一步被拦下了', { type: 'warning' })
      await load()
    } else {
      error.value = detail || e?.message || '保存失败'
    }
  } finally {
    saving.value = false
  }
}

/**
 * 重启确认。
 *
 * :param changedFrontend: 前端端口这次**变了**时传新端口 —— 那句话必须说出来：
 *   重启之后**这个页面就打不开了**，得去新地址。
 */
async function askRestart(changedFrontend = null) {
  const target = changedFrontend || info.value?.frontend || form.value.frontend
  const lines = []
  if (changedFrontend) {
    lines.push(
      `前端端口变了：重启后在 http://127.0.0.1:${target} 打开。`,
      '当前这个地址会失效，重启器会在新地址自动开一个页面（这个页面也要重新登录）。',
      '',
    )
  }
  lines.push(
    '节点、后端、前端会全部停掉再起一遍，大约十几秒。期间页面连不上，' +
      '恢复后需要重新登录一次（后端重启会换掉签名密钥）。',
    '数据、账号、文件、块全都不动 —— 端口与"哪台存了哪些块"无关。',
  )
  try {
    await ElMessageBox.confirm(lines.join('\n'), '立刻重启', {
      type: 'warning',
      confirmButtonText: '现在重启',
      cancelButtonText: '待会儿自己点',
    })
  } catch {
    return
  }
  restarting.value = true
  try {
    await deployApi.restart(null)
    ElMessage.success('正在重启，请稍候…')
    startPolling()
  } catch (e) {
    restarting.value = false
    error.value = e?.response?.data?.detail || e?.message || '重启失败'
  }
}

/** 重启之后该去哪儿等：前端端口没变就还是同源，变了就是新地址。 */
function pollTarget() {
  const p = info.value?.frontend
  if (!p) return ''
  return String(p) === String(window.location.port) ? '' : `http://127.0.0.1:${p}`
}

/**
 * 等后端回来。用 `fetch` 而不是项目里那个 axios 实例 —— 拦截器会给每次失败
 * 弹一个 toast，而这里注定要失败好几次（后端正在重启），那会刷一屏红字。
 */
function startPolling() {
  if (polling.value) return
  polling.value = true
  const target = pollTarget()
  const deadline = Date.now() + 180000
  const tick = async () => {
    if (Date.now() > deadline) {
      polling.value = false
      restarting.value = false
      error.value =
        '等了两分钟还没起来。看一眼新开的那个重启窗口，里面会说是哪一步卡住了。' +
        (target ? ` 也可以直接打开 ${target}/login 看看。` : '')
      return
    }
    try {
      // ★ 200 或 401 都算「它回来了」：后端每次启动都会随机生成一把 JWT
      //   签名密钥，所以重启之后旧令牌必然 401 —— 那**正是**起来了的样子。
      const r = await fetch(`${target}/api/status`, { cache: 'no-store' })
      if (r.ok || r.status === 401) {
        // 顺序不能反：先清会话再跳。守卫对"已登录访问 /login"是**送回首页**的，
        // 令牌还留着就会被弹回去，根本看不到登录页。
        auth.clear()
        ElMessage.success(target ? `重启完成，前端已在 ${target}` : '重启完成，请重新登录')
        setTimeout(() => window.location.replace(`${target}/login`), 800)
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

onMounted(() => {
  load()
  // 台数改了 → 这里的行数（每台一行）必须跟着重读。
  offBus = onDeployChanged(load)
})
onBeforeUnmount(() => {
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = null
  if (offBus) offBus()
  offBus = null
})

defineExpose({ refresh: load })
</script>

<template>
  <div class="panel mb-3" v-loading="loading">
    <div class="deploy-head">
      <h4 class="sec-title" style="margin-bottom: 0">端口</h4>
      <span class="mono text-2" style="font-size: 12px">
        现在跑着：后端 {{ running.backend ?? '未知' }} / 前端 {{ running.frontend ?? '未知' }}
        <template v-if="info"> · 配置 {{ info.backend }} / {{ info.frontend }}</template>
      </span>
    </div>

    <p class="note">
      这里改的是<b>下一次启动</b>绑哪几个端口：后端、前端、以及每一台存储节点各自的。
      改完<b>重启后生效</b>，点「立刻重启」即可，不用去按 cmd。
      端口被别的程序占着只提示、不拦（可以先存下来、过后再腾）；但两样东西
      <b>填了同一个端口</b>会被拦下来 —— 那一定起不来。
    </p>

    <div class="ports-grid">
      <div class="ports-row ports-head">
        <span>角色</span>
        <span>现在跑着</span>
        <span>重启后用</span>
        <span>探测</span>
      </div>

      <div class="ports-row">
        <span class="ports-name">后端</span>
        <span class="mono text-3">{{ running.backend ?? '未知' }}</span>
        <el-input-number
          v-model="form.backend"
          :min="info?.min ?? 1024"
          :max="info?.max ?? 65535"
          :step="1"
          :precision="0"
          :controls="false"
          size="small"
          :disabled="loading || saving || restarting"
          @change="invalidate"
        />
        <el-tag v-if="statusOf('backend', '后端')" :type="STATUS_TYPE[statusOf('backend', '后端')]" size="small">
          {{ STATUS_TEXT[statusOf('backend', '后端')] }}
        </el-tag>
        <span v-else class="text-3">—</span>
      </div>

      <div class="ports-row">
        <span class="ports-name">前端</span>
        <span class="mono text-3">{{ running.frontend ?? '未知' }}</span>
        <el-input-number
          v-model="form.frontend"
          :min="info?.min ?? 1024"
          :max="info?.max ?? 65535"
          :step="1"
          :precision="0"
          :controls="false"
          size="small"
          :disabled="loading || saving || restarting"
          @change="invalidate"
        />
        <el-tag v-if="statusOf('frontend', '前端')" :type="STATUS_TYPE[statusOf('frontend', '前端')]" size="small">
          {{ STATUS_TEXT[statusOf('frontend', '前端')] }}
        </el-tag>
        <span v-else class="text-3">—</span>
      </div>

      <div v-for="id in nodeIds" :key="id" class="ports-row">
        <span class="ports-name mono">{{ id }}</span>
        <span class="mono text-3">
          {{ runningNodes[id] ?? (singleProcess ? '单进程模式' : '未知') }}
        </span>
        <el-input-number
          v-model="form.nodes[id]"
          :min="info?.min ?? 1024"
          :max="info?.max ?? 65535"
          :step="1"
          :precision="0"
          :controls="false"
          size="small"
          :disabled="loading || saving || restarting"
          @change="invalidate"
        />
        <el-tag v-if="statusOf('node', id)" :type="STATUS_TYPE[statusOf('node', id)]" size="small">
          {{ STATUS_TEXT[statusOf('node', id)] }}
        </el-tag>
        <span v-else class="text-3">—</span>
      </div>
    </div>

    <div class="deploy-row mt-3">
      <el-button :disabled="loading || saving || restarting" :loading="probing" @click="checkPorts">
        检查占用
      </el-button>
      <el-button type="primary" :disabled="!dirty || restarting" :loading="saving" @click="save">
        保存
      </el-button>
      <el-button type="warning" :disabled="loading || saving" :loading="restarting" @click="askRestart()">
        <Icon name="refresh" :size="14" style="margin-right: 6px" />立刻重启
      </el-button>
      <el-button text :disabled="loading || saving || restarting" @click="useDefaults">填默认值</el-button>
      <el-button text :disabled="loading || saving || restarting" @click="load">刷新</el-button>
    </div>

    <div class="deploy-stats mono">
      <span>允许范围 <b>{{ info?.min ?? 1024 }}–{{ info?.max ?? 65535 }}</b></span>
      <span>
        默认值 后端 <b>{{ info?.defaults?.backend ?? '—' }}</b> / 前端
        <b>{{ info?.defaults?.frontend ?? '—' }}</b> / 节点从
        <b>{{ info?.defaults?.node_base ?? '—' }}</b> 起
      </span>
      <span v-if="singleProcess">当前是单进程模式：节点在同一个进程里，没有节点端口</span>
    </div>

    <el-alert v-if="error" type="error" :closable="false" class="mt-3" :title="error" />

    <el-alert
      v-if="localConflicts.length"
      type="error"
      :closable="false"
      class="mt-3"
      title="这几处填重了，保存会被拦下"
      :description="localConflicts.join('；')"
    />

    <el-alert
      v-if="info?.restart_required"
      type="warning"
      :closable="false"
      class="mt-3"
      title="改了端口，还没重启"
      :description="info.notice"
    />

    <el-alert
      v-if="(info?.notes || []).length"
      type="info"
      :closable="false"
      class="mt-3"
      title="配置里有几个端口用不了，已经改用别的（保存后就会写进配置）"
      :description="(info.notes || []).join('；')"
    />

    <el-alert
      v-if="occupied.length"
      type="warning"
      :closable="false"
      class="mt-3"
      title="有端口被别的程序占着"
      :description="
        occupied.map((x) => `${x.id} ${x.port}`).join('、') +
        ' 现在有别的程序在占用 —— 重启时这几样会起不来。可以换一个端口，或者先把占用它的程序停掉。'
      "
    />

    <p v-if="checked && !occupied.length" class="note mt-3" style="margin-bottom: 0">
      探测结论：这些端口现在都是空的（其中标「本系统在用」的是重启时会让出来的那几个）。
    </p>

    <div class="deploy-foot mt-3">
      <span class="text-3" style="font-size: 12px">
        端口存在 <span class="mono">{{ info?.source || 'nodes/deploy.json' }}</span>（与台数同一个文件）；
        「重置并启动.cmd」会把端口与台数一起复位成默认值。
      </span>
    </div>
  </div>
</template>

<style scoped>
/* ★ 本组件自己也要定一份这些类名：项目的 `scoped` 样式不会穿到子组件里，
   而这张卡片用的是与 NodeCountCard 同一套写法（`.panel` / `.note` 那些
   是全局样式，不用重复定义）。定义与 views/admin/dashboard 里那份保持一致。 */
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
/* 一行一个端口：角色 / 现在跑着 / 重启后用 / 探测结论。
   四列定宽 + 可滚动，行数跟着台数走（最多 32 行）。 */
.ports-grid {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.ports-row {
  display: grid;
  grid-template-columns: 96px 110px 132px 110px;
  align-items: center;
  gap: 10px;
  font-size: 12px;
}
.ports-head {
  color: var(--text-3);
  padding-bottom: 2px;
  border-bottom: 1px solid var(--border, rgba(128, 128, 128, 0.2));
}
.ports-name {
  color: var(--text-1);
}
</style>
