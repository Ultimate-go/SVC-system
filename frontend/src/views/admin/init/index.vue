<script setup>
/**
 * 初始化与系统自检 —— 管理员工作台下的第一页。
 *
 * ★ 2026-10-19（用户要求）：把「系统自检」整组搬进来，页面改名为
 *   **初始化与系统自检**，并改成**一次点击、两件事一起做**：
 *
 *   · 页面上先只摆一个按钮「初始化与系统自检」；
 *   · 点下去 → 初始化流水线（六张参数卡依次点亮）与十项一致性自检
 *     **并行**跑，跑完才把「全局运行态势 / 规模一览 / 系统参数 / 逐文件摘要 /
 *     密钥模型 / 部署模式」这些区块展开（小设计：没点之前不铺一屏参数，
 *     先把“点一下会发生什么”说清楚）；
 *   · 同一次登录只点一次：跑完按钮置为已完成，重进页面直接是展开态，
 *     那一组自检从本机记录还原（见 SelfCheck.vue）。
 *
 * 内容分四部分，对应「先把系统交代清楚，再开始干活」这件事：
 *
 * 0. **初始化与系统自检** —— 这一页的**第一件事**（登录管理员直接进这一页）：
 *    点一下，数据流把六张卡依次点亮 ——
 *    隐藏阶群 → 位置素数 → 分量与承诺 → 存储节点 → 端口与进程 → 落库与自检；
 *    同一拍里十项一致性自检并行跑（清单只在扫描时铺开，跑完收起成一行）。
 *    ★ 台数与端口**不在本页改** —— 它们在「存储节点」页的参数卡上（保存后重启生效）。
 * 1. **全局运行态势** —— 从文件进入系统到形成可验证证据的六段处理链路，
 *    点开任一阶段可看该段的密码学原理与关键式子。
 *    ★ 2026-10-19：整块由「总览」页**迁移**至此（连卡片与点开的知识页一起），
 *      原先这里的「算法与目的」三栏讲的是同一件事，已删。
 * 2. **初始化现状** —— 规模一览（文件 / 块 / 在线节点 / 用户 / 副本 / 副本缺口）、
 *    系统参数、逐文件摘要。数值全部来自 ``GET /api/system/status``。
 * 3. **部署模式与密钥模型** —— 两种部署形态的区别，以及两层密钥封装落在哪里。
 *
 * ★ 2026-10-18 整理（用户：太乱、去掉不精准的表述）：
 *   · 删掉「存储规模」整个区块 —— 文件数 / 总块数 / 副本数 / 副本不足四项
 *     与「规模一览」是同一组数字，一处看就够；
 *   · 「运行状态」只留部署形态（模式 / 传输 / 离线名单），不再重复在线台数与
 *     “集群稳定”——那是「规模一览」的判据，写在两处只会自相矛盾；
 *   · 健康灯只说它真的核过的两件事：「节点全部在线 · 副本达标」
 *     （原来写「系统运行正常」，把范围说大了 —— 一致性由自检回答）；
 *   · 「逐文件摘要」去掉一行「δ 指纹：每份各不相同」—— 每行文件自带指纹；
 *   · 修掉几处不精准的说法：δ 是公开量（不是“客户端只保存 δ”）、
 *     素数位长不等于 ℓ+1（新坐标下身份派生的是 128 位）、n_max 是老坐标的地址容量。
 *
 * ★ 2026-10-16：「系统自检」从「总览」页搬到这里。
 *   它回答的是「这套系统现在对不对」，与初始化/维护是同一组问题。
 *   ★ 2026-10-19 又改了一次形态：十项清单**直接铺在页面上**（随按钮一起跑），
 *   扫描中逐项点亮、跑完自动收起成一行，旁边留「再次自检」——自检可以反复跑，
 *   受“一次登录只能点一次”限制的只有初始化。
 *
 * 这一页的内容原先散在「总览」页上。总览改成展示运行链路与运行性能之后，它们统一搬到这里：
 * 这些是**部署与初始化**层面的量，与「现在跑得多快」不是一回事。
 *
 * 权限：``meta.requiresAdmin``，只有管理员进得来（后端接口本身对全体用户开放）。
 */
import { ref, computed, onMounted, nextTick } from 'vue'
import { systemApi } from '../../../api/system'
import { deployApi } from '../../../api/deploy'
import { initFlowPlayed, markInitFlowPlayed } from '../../../utils/session'
import { hexFp } from '../../../utils/format'
import StatCard from '../../../components/common/StatCard.vue'
import RingGauge from '../../../components/chart/RingGauge.vue'
import SelfCheck from '../../../components/security/SelfCheck.vue'
import InitFlow from '../../../components/chart/InitFlow.vue'
import ProcessingFlow from '../../../components/chart/ProcessingFlow.vue'
import Icon from '../../../components/icons/Icon.vue'

const loading = ref(true)
const error = ref('')
const status = ref(null)

const delta = computed(() => status.value?.delta || {})
const crs = computed(() => status.value?.crs || {})
const keywrap = computed(() => status.value?.keywrap || {})
const clusterHealthy = computed(() => !status.value?.nodes_down?.length && !status.value?.under_replicated)

/*
 * 存储槽位：显示成「已分配 / 容量」而不是裸一个数（2026-10-16 改）。
 *
 * ★ 分子 ``slots_allocated`` = 已分配过的槽位号总数（含删除留下的空洞，单调不减）。
 * ★ 分母 ``n_max`` = **素数表容量**（默认 10000）。它是**老坐标**文件的地址空间：
 *   那些文件的第 i 块取素数表第 ``offset+i`` 个；新坐标文件按**块身份**派生素数，
 *   不查那张表，因此不受这个容量约束。所以这个比值是“用掉了多少地址”，
 *   不是“还能存多少”—— 持久跑下去分母是会被超的，超了不报错。
 */
const slotsText = computed(() => {
  const used = delta.value.slots_allocated
  const cap = crs.value.n_max
  if (used == null || cap == null) return '—'
  return `${used}/${cap}`
})

/* 逐文件摘要默认只显示前几条，避免随文件数无限增长。 */
const FILE_PREVIEW = 4
const showAllFiles = ref(false)
const deltaFiles = computed(() => delta.value.files || [])
const visibleFiles = computed(() =>
  showAllFiles.value ? deltaFiles.value : deltaFiles.value.slice(0, FILE_PREVIEW),
)
const hiddenFileCount = computed(() => Math.max(0, deltaFiles.value.length - FILE_PREVIEW))

/**
 * 副本达标的块数，供环形仪表使用。
 *
 * 该仪表此前绘制「已用位置 / n_max」。位置上限已不存在（素数按块身份派生，
 * 槽位可无限增长），该比值失去意义。现绘制「副本达标的块数 / 总块数」，
 * 直接反映集群健康度。
 */
const replicaOk = computed(() =>
  Math.max(0, Number(status.value?.blocks || 0) - Number(status.value?.under_replicated || 0)),
)

/*
 * 「密钥模型」卡里这两行是**方案常量**，不是运行时状态：
 * 它们描述的是 core/keywrap.py 那一层的实现约定（算法、盐与密钥长度、迭代数、在哪解封），
 * 任何部署、任何时刻都相同，也不随请求变化，所以写在这里而不是每次轮询都由后端回传。
 * 改协议（换算法 / 改迭代数 / 改封装格式）时改这两段，并对照那一层的权威实现：
 * core/keywrap.py 里的 KIND_USER_KEY、PBKDF2_ITERATIONS、PRIVATE_KEY_BYTES。
 */
const KEY_SCHEME_TEXT = 'SM2-ECIES（封装块密钥）+ PBKDF2-HMAC-SM3（封装用户私钥）'
const KEY_STORAGE_TEXT =
  '库中仅存储私钥密文，不存储明文私钥或口令。' +
  '加密方式：口令经 PBKDF2-HMAC-SM3（盐 16 字节、迭代 20 万轮）派生出密钥加密钥（KEK，32 字节），' +
  '由其派生 SM4 密钥（16 字节），对 32 字节私钥标量做 SM4-CTR 加密，并附 32 字节完整性标签。'
const KEY_UNWRAP_TEXT =
  '解封在浏览器内完成：后端只返回私钥密文，口令在本地派生出 KEK，' +
  '完整性标签校验通过后解出私钥标量。私钥仅存于当前标签页的会话存储' +
  '（sessionStorage；闲置 5 分钟或关闭标签即清除），不写入 localStorage，也不随任何请求上传。' +
  '验证与解密均由浏览器执行。'

// 「模式 / 传输」取值说明：后端可能出现的取值 → 解释（manager.py 按部署配置返回）。
// mode 只有两档；transport 显示的是传输层类名，共三种（FaultyTransport 为故障演练代理，
// 首次演练时装上并持续生效，重启后恢复为真实传输）。未知取值走兜底文案。
const MODE_DESC = {
  distributed: '跨进程分布式：协调者通过 HTTP 与多个独立节点进程通信，分块与副本分散在多台节点上，不存在单点持有全部数据。',
  'single-process': '单进程演示：协调者与各节点同处一个 Python 进程内，存储层为进程内直连、不经网络，仅用于本地演示与自测。',
}
const TRANSPORT_DESC = {
  LocalTransport: '进程内直连：单进程模式下由存储层自建的本地传输，调用不经过网络。',
  HttpTransport: '跨进程 HTTP：协调者经 HTTP 加访问令牌访问各节点服务，带重试、退避与超时配置。',
  FaultyTransport: '故障演练代理：包在真实传输外层，可注入节点掉线或永久损毁两类故障，用于验证部分节点失效时副本与验证仍然成立；安装后持续生效，重启恢复为真实传输。',
}
const modeDesc = computed(() => MODE_DESC[status.value?.mode] || '当前部署模式未收录说明，请结合部署文档确认该取值。')
const transportDesc = computed(() => TRANSPORT_DESC[status.value?.transport] || '当前传输层未收录说明；它由后端按部署配置自动选择。')

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
  await loadDeploy()
}

/* ---------------------------------------------------------------------------
   ⓪ 初始化流水线
   ---------------------------------------------------------------------------
   ★ 这一页是演示的**第一站**（登录管理员直接进这里）：一个「初始化与系统自检」按钮，
     点下去把"这套系统的参数是怎么定下来的"演一遍。

   ★ **每次登录只演一遍**：告一段落之后按钮就变成「初始化已完成」且不可点
     （同一次登录里再进这一页，直接呈现完成态）。判定靠 utils/session.js 里
     那对登录标记（登录时盖、演完盖，两枚相等就算演过）。退出重登又会重新可演。

   ★ 参数分两类（这条流水线只**如实展示**，不改任何一个）：

     · 能由你决定的：存储节点台数、各节点端口、后端/前端端口
       —— 在 **管理员工作台 → 存储节点** 那两张卡上改（保存后重启生效）；
     · 部署时定死的：隐藏阶群位数 |N|、分量位长 ℓ、素数表容量 n_max、分块大小区间。
       它们决定 CRS 与位置素数，改一次等于重建整套系统，所以不提供修改。
--------------------------------------------------------------------------- */
const flowRef = ref(null)
const deploy = ref(null)
const ports = ref(null)
/** 正在放（防连点）。 */
const initRunning = ref(false)
/** 已点过（或本次登录早先点过）—— 点过之后才展开页面其余区块。 */
const started = ref(false)
/** 自检子组件（十项清单）：由这里的按钮触发，它自己不放按钮。 */
const selfCheckRef = ref(null)
/** 已跑完（或本次登录早先已跑过）——按钮置为已完成且不可点。 */
const initDone = ref(false)

/** 台数与端口（仅管理员接口；本页本身就是 requiresAdmin，失败就当没有）。 */
async function loadDeploy() {
  try {
    const [d, p] = await Promise.all([deployApi.get(), deployApi.ports()])
    deploy.value = d.data
    ports.value = p.data
  } catch {
    /* 拿不到就不填 —— 卡片上会显示「—」，不编数字 */
  }
}

const nodeIds = computed(() => deploy.value?.node_ids || ports.value?.node_ids || [])
const nodeCount = computed(() => deploy.value?.node_count ?? status.value?.nodes ?? 0)
const portList = computed(() => {
  const map = ports.value?.nodes || {}
  const vals = nodeIds.value.map((id) => map[id]).filter((x) => x != null)
  return vals.length ? vals.join(', ') : '—'
})

/**
 * 六张卡。参数值一直都在（不靠任何"选择"）—— 卡片露不露、什么时候长开，
 * 由 InitFlow 的展开动效决定：播到哪一张，哪一张就把参数露出来。
 */
const flowSteps = computed(() => {
  const st = status.value || {}
  const c = crs.value
  const rows = (list) => list
  return [
    {
      key: 'group', no: 1, title: '隐藏阶群', en: 'GROUP',
      rows: rows([
        { k: '模数 |N|', v: `${c.N_bits ?? '—'} 位（演示参数，不提供实际安全强度）` },
        { k: '生成方式', v: '随机取 p、q，算出 N = p·q 后立即销毁 p、q' },
        { k: '公开量', v: 'N 、 g ' },
      ]),
      note: '群阶未知，指数运算公开可做，核心难度在求 e 次根。',
    },
    {
      key: 'primes', no: 2, title: '位置素数', en: 'PRIMEGEN',
      rows: rows([
        { k: '素数位长', v: `${c.prime_bits ?? '—'} 位（分量长度ℓ + 1）` },
        { k: '派生方式', v: '由「所有者 + 文件标识 + 块号」哈希到素数，每块独立身份素数' },
        // ★ 不能只写「= 分量位长 + 1」：那只会对**老坐标**（全局素数表）成立。
        //   新坐标（默认）下素数由块身份哈希派生，实际 128 位（见 svc/mathbase.py::hash_prime）。
        { k: '素数表容量 n_max', v: `${c.n_max ?? '—'}` },
      ]),
      note: '位置由块的身份派生，因此位置不需要全局分配，删除留下的空洞也不必回收。',
    },
    {
      key: 'commit', no: 3, title: '分量与承诺', en: 'COMMIT',
      rows: rows([
        { k: '分量 v_i', v: 'SM3(该块密文)' },
        { k: '分量位长 ℓ', v: `${c.l ?? '—'} 位` },
        { k: '承诺 δ', v: '((U, C), n)' },
      ]),
      note: '证据恒为两个群元素（≤ 256 字节），与文件大小无关。',
    },
    {
      key: 'nodes', no: 4, title: '存储节点', en: 'NODES',
      rows: rows([
        { k: '台数', v: `${nodeCount.value} 台${deploy.value?.restart_required ? '（配置值，重启后生效）' : ''}` },
        { k: '副本因子', v: `${st.replica_factor ?? '—'} 份 / 块` },
        {
          k: '节点',
          v: nodeIds.value.length
            ? `${nodeIds.value[0]} … ${nodeIds.value[nodeIds.value.length - 1]}（共 ${nodeIds.value.length} 台）`
            : '—',
        },
      ]),
      // ★ 不写「每台一个独立进程」：那只对跨进程部署成立；单进程演示模式下
      //   各节点同处一个 Python 进程、存储为进程内直连（见 deploy 的 mode / transport）。
      note: '每台存储节点维护自己的向量视图；跨进程部署时各为一个独立进程。',
    },
    {
      key: 'ports', no: 5, title: '端口与进程', en: 'PORTS',
      rows: rows([
        { k: '后端 / 前端', v: `${ports.value?.backend ?? '—'} / ${ports.value?.frontend ?? '—'}` },
        { k: '节点端口', v: portList.value },
      ]),
      note: '改台数与端口已写入 nodes/deploy.json，重启后生效（在「存储节点」页改）。',
    },
    {
      key: 'persist', no: 6, title: '落库与自检', en: 'PERSIST',
      rows: rows([
        { k: '落库', v: 'CRS、位置账目与账号写入 vds.db' },
        { k: '账号', v: `${keywrap.value.users ?? 0} 个（私钥以口令封装，服务器不持有明文）` },
        { k: '自检', v: '十项一致性核对 —— 见上方「逐项核对集群与向量一致性」' },
      ]),
      note: '登记表 / 节点视图 / 增量摘要三者一致，初始化完成',
    },
  ]
})

/**
 * 点「初始化与系统自检」：两件事同时跑。
 * 跑完盖戳（utils/session.js）+ 置为已完成 —— 同一次登录里按钮不再可点。
 */
async function startInit() {
  if (initDone.value || initRunning.value) return
  initRunning.value = true
  // ★ 先展开：流水线六张卡与十项清单得先挂上去，动画才播得起来
  started.value = true
  await nextTick()
  // ★ 两条动效并行：一条讲“参数怎么定下来的”，一条核对“现在账实是否一致”。
  await Promise.all([flowRef.value?.play(), selfCheckRef.value?.run()])
  initRunning.value = false
  initDone.value = true
  markInitFlowPlayed()
}

/** 本次登录早先已经跑过 ⇒ 直接呈现完成态（卡片全部长开、自检结果从本机还原）。 */
async function showFinished() {
  started.value = true
  initDone.value = true
  await nextTick()
  flowRef.value?.finish()
}

onMounted(async () => {
  await load()
  // ★ 每次登录只演一遍：演过就直接给完成态
  if (initFlowPlayed()) showFinished()
})
</script>

<template>
  <div v-if="loading" class="loading" v-loading="true" />
  <div v-else-if="error" class="panel">
    <p class="text-danger">{{ error }}</p>
    <el-button class="mt-3" @click="load">重试</el-button>
  </div>
  <div v-else-if="status" class="init-page">

    <!-- ⓪ 初始化与系统自检：这一页的第一件事 —— 点一次，两件事一起跑 -->
    <div class="section-heading">
      <div><span class="section-kicker mono">INIT &amp; SELF-CHECK</span><h2>初始化与系统自检</h2></div>
      <span class="section-note mono">ONE CLICK</span>
    </div>
    <section class="panel flow-panel">
      <div class="flow-actions">
        <div class="flow-buttons">
          <el-button
            type="primary"
            :loading="initRunning"
            :disabled="initDone || initRunning"
            @click="startInit"
          >
            <Icon v-if="!initRunning" :name="initDone ? 'check' : 'key'" :size="13" style="margin-right: 6px" />
            {{ initDone ? '初始化与系统自检已完成' : '初始化与系统自检' }}
          </el-button>
        </div>
      </div>

      <InitFlow v-if="started" ref="flowRef" :steps="flowSteps" />

      <!-- 十项一致性自检：上面那个按钮点一次会连带跑一遍；
           跑完之后**自检自己还有「再次自检」**（初始化只能一次，自检可以无限次）。
           :restore 只在“本次登录早先已跑过”时为真 —— 点按钮那一瞬不要还原旧结论。 -->
      <div v-if="started" class="selfcheck-block">
        <SelfCheck ref="selfCheckRef" :restore="initDone" />
      </div>

      <p v-if="initDone" class="flow-done">
        <span class="mono flow-done-tag">READY</span>
        初始化已完成、十项自检完成，参数已依次写入并落库，逐项现状见下方区块。
      </p>
    </section>

    <!-- ① 全局运行态势：处理链路（★★ 2026-10-19 由总览页**整体迁入**）
         ★ 原「算法与目的」一整块（这套系统做什么 / 算法的骨架 / 目的）已删 ——
           它讲的正是这条六段链路与它的原理，与卡片里的知识页内容重复；
           现在原理改为**点开卡片**看（见 components/chart/ProcessingFlow.vue）。 -->
    <template v-if="started">
    <div class="section-heading">
      <div><span class="section-kicker mono">SYSTEM OVERVIEW</span><h2>全局运行态势</h2></div>
      <span class="section-note mono">IAVC-VDSS</span>
    </div>
    <section class="panel hero-panel">
      <p class="hero-lead">
        从文件进入系统到形成可验证证据的完整数据链路。点开任一阶段，可看该段的密码学原理与关键式子。
      </p>
      <ProcessingFlow />
    </section>

    <!-- ② 初始化现状：规模 + 系统参数 + 逐文件摘要（自检在顶部 ⓪ 里）。
         ★ 原来这里还有一个「存储规模」区块，四项（文件数 / 总块数 / 副本数 / 副本不足）
           与下面这一排完全重复，已合并。 -->
    <div class="section-heading section-heading-spaced">
      <div><span class="section-kicker mono">INITIALIZED SCALE</span><h2>规模一览</h2></div>
    </div>
    <section class="panel scale-panel">
      <div class="scale-row">
        <div class="scale-item">
          <b class="mono">{{ status.files || 0 }}</b><span>份文件</span>
        </div>
        <div class="scale-item">
          <b class="mono">{{ status.blocks || 0 }}</b><span>个块</span>
        </div>
        <div class="scale-item">
          <b class="mono">{{ status.nodes || 0 }}</b><span>台节点在线</span>
        </div>
        <div class="scale-item">
          <b class="mono">{{ keywrap.users || 0 }}</b><span>个用户</span>
        </div>
        <div class="scale-item">
          <b class="mono">{{ status.replica_factor || 0 }}</b><span>份副本 / 块</span>
        </div>
        <div
          class="scale-item"
          :title="status.under_replicated ? '开副本之前上传的历史数据，重新上传可补齐' : '当前没有缺口'"
        >
          <b class="mono">{{ status.under_replicated || 0 }}</b><span>块副本不足</span>
        </div>
        <!-- ★ 这一栏只说它真的核对过的两件事：节点全在线、副本达标。
             以前写「系统运行正常」是把范围说大了 —— 一致性由上面那十项自检回答。 -->
        <div class="health-pill" :class="clusterHealthy ? 'is-ok' : 'is-warn'">
          <span class="health-dot" />
          <span>{{ clusterHealthy ? '节点全部在线 · 副本达标' : '有节点离线或缺副本' }}</span>
        </div>
      </div>
    </section>

    <!-- 系统参数：系统“长什么样”（一致性在顶部那十项里看） -->
    <div class="section-heading section-heading-spaced">
      <div><span class="section-kicker mono">PUBLIC PARAMETERS</span><h2>系统参数</h2></div>
    </div>
    <div class="grid">
      <StatCard
        hint-top
        label="公开参数"
        :value="crs.N_bits"
        unit="位"
        hint="隐藏阶群的模数位长；本部署为演示参数，不提供实际安全强度"
      />
      <StatCard hint-top label="分量位长" :value="crs.l" unit="位" hint="一个公开分量 v_i 的比特数" />
      <StatCard
        hint-top
        label="素数位长"
        :value="crs.prime_bits"
        unit="位"
        hint="方案参数 ℓ+1；新坐标下按块身份哈希派生的素数实际为 128 位"
      />
      <!-- 原「位置上限 = n_max」已不适用：素数按块身份派生，块数无上限。
           此处显示「已分配槽位 / 素数表容量」：分子含删除留下的空洞（故 >= 块数），
           分母是老坐标文件的地址容量 —— 它**不是**块数上限（见上面的 computed）。 -->
      <StatCard
        hint-top
        label="已分配槽位"
        :value="slotsText"
        hint="含删除留下的空洞；分母 n_max 只是老坐标文件的地址容量，不是上限"
      />
    </div>

    <!-- 自检已在顶部 ⓪ 那块里（与初始化同一个按钮触发）：这里不再单留一块。 -->

    <div class="compact-sections">
      <section class="compact-section">
        <div class="section-heading">
          <div><span class="section-kicker mono">COMMITMENTS</span><h2>逐文件摘要 <span class="mono">每份文件一条向量</span></h2></div>
        </div>
        <div class="panel commitment-panel">
          <div class="panel-title-row"><span class="live-mark"><i />SYNCED</span></div>
          <div class="delta-row">
            <RingGauge :value="replicaOk" :max="status.blocks || 1" label="副本达标" />
            <div class="delta-detail">
              <div class="delta-item"><span class="k">n</span><span class="v mono">{{ delta.n }}</span><span class="hint">在用块数（各文件之和）</span></div>
              <!-- 摘要为逐文件粒度，不存在单一全局 U / C。
                   「δ 指纹」那一项已删：下面每一行文件自带自己的指纹，不必再摆一个“每份各不相同”。 -->
              <div class="delta-item"><span class="k">向量</span><span class="v mono">{{ deltaFiles.length }}</span><span class="hint">每份文件一条</span></div>
            </div>
          </div>
          <p class="fp-note">指纹是文件公开承诺 δ 的 12 位十六进制短摘要（sha256("U:C:n") 的前 12 位），用于在不暴露明文的前提下快速识别与核对文件；悬浮可查看该文件承诺的完整 U 与 C。</p>
          <div
            v-for="f in visibleFiles"
            :key="`${f.owner}/${f.file_key}`"
            class="panel-subtitle mono file-line"
            style="white-space: normal; margin-top: 6px"
            :title="'U = ' + f.U + '\nC = ' + f.C"
          >
            （{{ f.owner }}）{{ f.file_key }} n={{ f.n }} 指纹 {{ hexFp(f.delta_fp, 12) }}
          </div>
          <button v-if="hiddenFileCount" type="button" class="more-toggle" @click="showAllFiles = !showAllFiles">
            {{ showAllFiles ? '收起' : `展开其余 ${hiddenFileCount} 条` }}
          </button>
        </div>
      </section>

      <section class="compact-section">
        <div class="section-heading">
          <div><span class="section-kicker mono">KEY MANAGEMENT</span><h2>密钥模型</h2></div>
          <span class="section-note mono">NO MASTER KEY</span>
        </div>
        <div class="panel key-panel">
          <div class="keywrap">
            <div class="keywrap-item"><span class="k">方案</span><span class="v">{{ KEY_SCHEME_TEXT }}</span></div>
            <div class="keywrap-item"><span class="k">用户 / 持有密钥：</span><span class="v mono">{{ keywrap.users }} / {{ keywrap.user_keys }}</span></div>
            <div class="keywrap-item"><span class="k">私钥存储位置</span><span class="v">{{ KEY_STORAGE_TEXT }}</span></div>
            <div class="keywrap-item"><span class="k">解封位置</span><span class="v">{{ KEY_UNWRAP_TEXT }}</span></div>
          </div>
        </div>
      </section>

      <!--
        ★ 部署模式单独占一整行窄条：它现在只剩三行字（模式 / 传输 / 离线名单），
          再和「逐文件摘要」并排就会是一高一矮两张卡，看着乱。
      -->
      <section class="compact-section compact-section--full">
        <div class="section-heading">
          <div><span class="section-kicker mono">DEPLOYMENT</span><h2>部署模式</h2></div>
        </div>
        <div class="panel health-panel">
          <div class="cluster-row">
            <span class="cluster-item">模式 <b class="mono">{{ status.mode }}</b></span>
            <span class="cluster-item">传输 <b class="mono">{{ status.transport }}</b></span>
            <span v-if="status.nodes_down?.length" class="cluster-item text-danger">
              离线：<b class="mono">{{ status.nodes_down.join(', ') }}</b>
            </span>
          </div>
          <p class="detail-line"><b>模式</b>：{{ modeDesc }}</p>
          <p class="detail-line"><b>传输</b>：{{ transportDesc }}</p>
        </div>
      </section>
    </div>
    </template>
  </div>
</template>

<style scoped>
.loading {
  min-height: 200px;
}
.section-heading { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; margin: 18px 1px 10px; }
.section-heading-spaced { margin-top: 27px; }
.section-heading h2 { color: var(--text-1); font-size: 19px; font-weight: 600; margin: 5px 0 0; }
.section-kicker { color: var(--accent); font-size: 13px; letter-spacing: .13em; }
.section-note { color: var(--text-3); font-size: 13px; letter-spacing: .08em; }

/* ---- 全局运行态势（原总览页的 hero 面板，2026-10-19 整体迁入） ---- */
.hero-panel {
  position: relative;
  overflow-x: hidden;
  padding: 16px 18px 18px;
  background: radial-gradient(circle at 88% 8%, color-mix(in srgb, var(--accent) 9%, transparent), transparent 34%), var(--bg-panel);
}
.hero-lead { color: var(--text-2); font-size: 13px; line-height: 1.75; margin: 0 0 12px; }

/* ---- 规模一览 ---- */
.scale-panel { padding: 18px; }
.scale-row { display: flex; flex-wrap: wrap; align-items: center; gap: 14px 40px; }
.scale-item { display: flex; flex-direction: column; gap: 2px; }
.scale-item b { color: var(--text-1); font-size: 30px; font-weight: 500; line-height: 1.15; }
.scale-item span { color: var(--text-3); font-size: 13px; }
.health-pill {
  margin-left: auto;
  display: inline-flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid var(--line);
  border-radius: 999px;
  color: var(--text-1);
  font-size: 14px;
  white-space: nowrap;
  background: color-mix(in srgb, var(--bg-raised) 78%, transparent);
}
.health-dot, .live-mark i { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 0 4px color-mix(in srgb, var(--ok) 12%, transparent); }
.health-pill.is-warn .health-dot { background: var(--warn); box-shadow: 0 0 0 4px color-mix(in srgb, var(--warn) 12%, transparent); }
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}
/* 指标卡内文字字号整体上调，与页面其余文字保持均衡（仅作用于本页，不改共享组件基值） */
.grid :deep(.stat-card .label) { font-size: 14px; }
.grid :deep(.stat-card .value) { font-size: 30px; }
.grid :deep(.stat-card .unit) { font-size: 15px; }
.grid :deep(.stat-card .hint) { font-size: 13px; }

.compact-sections { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 18px 16px; margin-top: 27px; }
.compact-section { min-width: 0; display: flex; flex-direction: column; }
.compact-section--full { grid-column: 1 / -1; }
.compact-section .section-heading { margin-top: 0; min-height: 43px; }
.compact-section > .panel { flex: 1 1 auto; min-height: 262px; box-sizing: border-box; }
.compact-section--full > .panel { min-height: 0; height: auto; }
.commitment-panel { height: auto; min-height: 262px; }
/* 部署模式现在是整行窄条，不再需要和左栏等高。 */
.health-panel { height: auto; min-height: 0; }
.commitment-panel .delta-row { min-height: 112px; }
.panel-title-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 15px; }
.panel-subtitle { color: var(--text-2); font-size: 15px; }
.live-mark { display: inline-flex; align-items: center; gap: 7px; color: var(--text-3); font-size: 13px; letter-spacing: .08em; }
.live-mark i { display: inline-block; width: 5px; height: 5px; }
.more-toggle { align-self: flex-start; margin-top: 10px; padding: 3px 0; color: var(--accent); font: inherit; font-size: 15px; background: none; border: 0; cursor: pointer; }
.more-toggle:hover { text-decoration: underline; }
.delta-row { display: flex; gap: 28px; align-items: center; }
.delta-detail { display: flex; flex-direction: row; flex-wrap: wrap; gap: 16px 32px; flex: 1; }
.delta-item { display: flex; flex-direction: column; align-items: flex-start; gap: 3px; }
.delta-item .k { font-family: var(--font-mono); color: var(--accent); font-weight: 500; font-size: 13px; }
.delta-item .v { color: var(--text-1); font-size: 17px; }
.delta-item .hint { color: var(--text-3); font-size: 14px; }
.fp-note { color: var(--text-3); font-size: 14px; line-height: 1.6; margin: 10px 0 2px; }
.file-line { color: var(--text-2); font-size: 15px; }
.cluster-row { display: flex; flex-wrap: wrap; gap: 24px; font-size: 15px; color: var(--text-2); }
/* 部署模式：模式/传输两行释义跟在取值行后面，拉开一点距离。 */
.cluster-row + .detail-line { margin-top: 16px; }
.detail-line { color: var(--text-2); font-size: 14px; line-height: 1.7; margin: 0 0 8px; }
.detail-line:last-child { margin-bottom: 0; }
.detail-line b { color: var(--text-1); }

/*
 * 标签列 + 值列：四项都是「标签在左、值在右」，值一律从**同一条竖线**开始。
 * 标签列宽取最宽的那个标签（max-content），所以「方案」「用户 / 持有密钥：」
 * 「私钥存储位置」「解封位置」后面的内容左边缘对齐。
 * .keywrap-item 用 display: contents 摊平，让 .k / .v 直接当 grid 的两列；
 * align-items: baseline 保证长值换行时，值的第一行仍与标签同一基线。
 */
.keywrap {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  gap: 8px 14px;
  align-items: baseline;
  font-size: 15px;
}
.keywrap-item { display: contents; }
.keywrap-item .k { color: var(--text-3); white-space: nowrap; }
.keywrap-item .v { color: var(--text-1); }

@media (max-width: 900px) {
  .compact-sections { grid-template-columns: 1fr; }
  .compact-section > .panel, .commitment-panel, .health-panel, .key-panel { height: auto; min-height: 0; }
  .health-pill { margin-left: 0; }
}
/* 大屏（>= 1500px）：限制卡片宽度，避免单行文本过长。 */
@media (min-width: 1500px) {
  .init-page { max-width: 1560px; margin-left: auto; margin-right: auto; }
  .grid { grid-template-columns: repeat(auto-fill, minmax(216px, 1fr)); }
  .compact-sections { gap: 22px 20px; }
}
@media (max-width: 560px) {
  .delta-row { gap: 16px; }
  .delta-item { gap: 8px; }
  .delta-item .hint { display: none; }
  .section-heading { align-items: flex-start; }
  .section-note { padding-top: 4px; }
}
/* 十项一致性自检：接在流水线下面，用一条虚线跟“参数怎么定下来的”分开。 */
.selfcheck-block { margin-top: 16px; padding-top: 16px; border-top: 1px dashed var(--line); }

/* ---- ⓪ 初始化流水线：选参数的两个按钮 + 那一句结论 ---- */
.flow-panel { padding-top: 16px; }
.flow-actions {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 14px;
}
.flow-buttons { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.flow-done {
  margin: 12px 0 2px;
  padding-top: 11px;
  border-top: 1px dashed var(--line);
  color: var(--text-2);
  font-size: 13px;
  line-height: 1.7;
}
.flow-done-tag {
  display: inline-block;
  margin-right: 8px;
  padding: 1px 7px;
  border: 1px solid color-mix(in srgb, var(--ok) 45%, transparent);
  border-radius: 999px;
  color: var(--ok);
  font-size: 11px;
  letter-spacing: .12em;
}

@media (max-width: 700px) {
  .flow-actions { flex-direction: column; gap: 12px; }
}
</style>
