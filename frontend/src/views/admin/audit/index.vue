<script setup>
/**
 * 审计流水。
 *
 * - GET /api/admin/audit —— **服务端分页**，返回 {items,total,page,page_size,pages}。
 *   默认**每页 10 条**，可选 10/20/50；支持页码 + 「跳到第 N 页」。
 *   过滤条件：
 *     target   精确匹配「所有者/文件标识」—— 回放某一份文件的完整经历
 *     actor    精确匹配操作者 —— 查询类动作只能靠它（它们不挂在某个文件上）
 *     action   精确匹配动作名（下拉，选项来自库里真出现过的值）
 *     ok       只看成功 / 只看被拒
 *     since/until  时间窗（**本地**时间，半开区间）—— 换算成 UTC 是后端的事
 *     q        模糊匹配（actor / action / target / detail / remark）
 * - **折叠**：默认只显示"时间 / 人 / 动作 / 对象 / 状态"这一行摘要；
 *   detail、备注、备注编辑框都要点开才出现。顶部有「全部展开/收起」。
 * - 被拒的动作（ok:false）要显眼标出。
 * - **备注**：管理员可以给任意一条流水写人工批注（PATCH /audit/{id}/remark）。
 *   备注与 detail **分开显示** —— detail 是系统当时记下的事实（"不是所有者"），
 *   备注是人后来的解释（"演示用的，不是故障"）。混在一起就分不清谁说的了。
 * - 只记元数据，永远不含明文/密钥/密文。
 *
 * ★ 时间过滤为什么把「本地值」原样发给后端、不在前端转 UTC：
 *   库里存的是 UTC（见 backend/models.py:utcnow）。前端要转 UTC 就得自己算
 *   时区偏移 —— 而这个项目**刚修过**一个 8 小时显示的 bug（format.js:fmtTime），
 *   起因正是"前端把无时区后缀的 UTC 串当本地时间"。中心换算只剩**一处**要做对。
 *   所以这里 ``value-format="YYYY-MM-DDTHH:mm:ss"`` 给出本地墙上时间，原样发走。
 *
 * ★ 展开状态用 ``Set<id>`` 而不是"按下标"或"塞进行对象"：
 *   · 用下标 —— 翻页后下标会串味（每页都有第 3 行）；
 *   · 塞进行对象（``r._expanded = true``）—— 重查/翻页会整个换掉 rows，
 *     状态会跟着旧数据一起消失，换页回来发现"刚展开的又收起来了"。
 *   用 id 做键，语义才是"这一条流水"而不是"这一页的这一格"。
 *   ★ 但**换页后仍要 prune**：留着上一页的 id 只会让集合越涨越大，没有意义。
 */
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { systemApi } from '../../../api/system'
import { fmtTime } from '../../../utils/format'
import PageHeader from '../../../components/common/PageHeader.vue'
import EmptyState from '../../../components/common/EmptyState.vue'

const loading = ref(false)
const error = ref('')
const rows = ref([])

//: 筛选条件。**空值一律不发**（见 buildParams）——发空串会让后端把
//: 它当成"要匹配空字符串"，结果是永远查不到东西。
const filters = reactive({
  target: '',
  actor: '',
  action: '',
  status: '', // '' | 'ok' | 'denied'
  q: '',
})
//: 时间窗用独立两个字段更好用 —— el-date-picker 的 daterange 给数组，
//: 但"只填开始"或"只填结束"也是常见需求，所以给**两个独立的 datetime**。
const since = ref('')
const until = ref('')

//: 动作下拉的选项 —— 从库里的真值拉，不写死（见后端 /audit/actions）。
const actionOptions = ref([])

//: 正在编辑备注的那条 id（同一时刻只开一个输入框，免得满屏都是框）。
const editingId = ref(null)
//: 编辑框里的草稿，按 id 存 —— 关掉再打开不该把写了一半的字丢掉。
const drafts = reactive({})
const savingId = ref(null)

//: 分页信封（后端给的那几个数落在这里）。默认**每页 10 条、从第 1 页开始**。
const pager = reactive({ total: 0, page: 1, pageSize: 10, pages: 1 })
//: 用户选了「跳转到第 N 页」时先落在这个输入框里，点「跳转」才生效 ——
//: 直接绑 page 会让"输入到一半（比如想输 12，刚打完 1）"就触发一次加载。
const jumpTo = ref(null)

//: 每行是否展开。用 `Set<id>` 而不是下标 —— 翻页后下标会串味
//: （第 1 页第 3 行的下标是 2，第 2 页第 3 行的下标也是 2，收起哪个就说不清了）。
const expanded = ref(new Set())
//: 模板里读它（`expanded` 这个名字留给 ref 本身，模板里统一叫 expandedSet 更清楚）。
const expandedSet = computed(() => expanded.value)
//: 已经被"自动展开"过的 id。被拒记录首次出现时会自动展开一次，之后就归用户管 ——
//: 用户若手动收起，换页/重查回来**不该**又被强行撑开，所以必须留这份痕迹。
//: 只增不减（翻页时随 pruneExpanded 一起清掉不在本页的）。
const autoExpanded = ref(new Set())
//: 一条流水有没有"可展开的内容"。没有的话不显示展开箭头 ——
//: 一个点开什么都没有的箭头比没有箭头更让人困惑。
function hasMore(r) {
  return !!(r.detail || r.remark)
}

/** 被拒的记录默认展开 —— 拒绝原因是这条记录的重点，不该藏在折叠里。
 *  ★ 只对**首次出现**的行生效：已在 `autoExpanded` 里的 id 不再动，
 *    否则用户手动收起后，load() 一跑又会被重新撑开。 */
function autoExpandDenied() {
  const opened = autoExpanded.value
  let exp = null // 懒克隆：只有真的需要展开时才换新 Set
  let seen = null // 同上，避免无谓地触发 autoExpanded 的响应式
  for (const r of rows.value) {
    if (r.ok || !hasMore(r) || opened.has(r.id)) continue
    if (!exp) exp = new Set(expanded.value)
    exp.add(r.id)
    if (!seen) seen = new Set(opened)
    seen.add(r.id)
  }
  // ★ 换新实例再赋回（Set 原地 add 不触发 Vue 响应式）
  if (exp) expanded.value = exp
  if (seen) autoExpanded.value = seen
}

//: 折叠状态下**摘要行**要不要显示 detail 的前一段？
//: 这里刻意不显示：一屏 10 条，每条都拖一行小字，扫视时反而看不清状态色块。
//: 摘要行只给"时间 / 人 / 动作 / 对象 / 状态"。
//: 例外：**被拒**的记录默认整行展开（拒绝原因才是重点，见 autoExpandDenied）。
function toggle(r) {
  const s = expanded.value
  // ★ 必须换一个新 Set 再赋回：直接 s.add/delete 不会触发 Vue 的响应式
  //   （Set 的增删不是 Proxy 能拦住的那些操作，浅层 ref 追踪不到）。
  const next = new Set(s)
  if (next.has(r.id)) next.delete(r.id)
  else next.add(r.id)
  expanded.value = next
}

/** 这一页是不是**全都**展开了 —— 用来决定按钮写"全部展开"还是"全部收起"。 */
const allExpanded = computed(() => {
  const list = rows.value.filter(hasMore)
  if (!list.length) return false
  return list.every((r) => expanded.value.has(r.id))
})

function toggleAll() {
  const next = new Set(expanded.value)
  if (allExpanded.value) {
    for (const r of rows.value) next.delete(r.id)
  } else {
    for (const r of rows.value) if (hasMore(r)) next.add(r.id)
  }
  expanded.value = next
}

/** 动作名的中文标签。库存的仍是英文名（后端不该管界面怎么叫它）。 */
const ACTION_LABEL = {
  login: '登录',
  logout: '登出',
  upload: '上传',
  append_file: '追加',
  modify_block: '改块',
  truncate_file: '截断',
  delete_file: '删除文件',
  zero_block: '清零块',
  decrypt: '解密',
  decrypt_denied: '解密被拒',
  query: '查询',
  query_files: '查文件列表',
  verify_batch: '批量验证',
  retry_push: '补推副本',
  user_create: '建用户',
  user_patch: '改用户',
  user_delete: '删用户',
  check: '自检',
  deploy_change: '改台数',
  deploy_restart: '重启',
  deploy_redistribute: '补副本',
  deploy_ports: '改端口',
  audit_remark: '写备注',
}
function actionLabel(a) {
  return ACTION_LABEL[a] || a
}

/** 收集非空筛选项 → 请求参数。空串/空数组/未选都**不发**。 */
function buildParams() {
  const params = { page: pager.page, page_size: pager.pageSize }
  const t = filters.target.trim()
  const a = filters.actor.trim()
  const act = filters.action
  const q = filters.q.trim()
  if (t) params.target = t
  if (a) params.actor = a
  if (act) params.action = act
  if (filters.status === 'ok') params.ok = true
  else if (filters.status === 'denied') params.ok = false
  if (since.value) params.since = since.value
  if (until.value) params.until = until.value
  if (q) params.q = q
  return params
}

/** 当前是否处于"有筛选"状态 —— 用于「重置」按钮的高亮与空态文案。 */
const hasFilter = computed(
  () =>
    !!(filters.target.trim() || filters.actor.trim() || filters.action ||
       filters.status || filters.q.trim() || since.value || until.value),
)

/** 一句话说清"现在筛的是什么"，显示在结果数旁边。 */
const filterSummary = computed(() => {
  const bits = []
  if (filters.target.trim()) bits.push(`对象=${filters.target.trim()}`)
  if (filters.actor.trim()) bits.push(`操作者=${filters.actor.trim()}`)
  if (filters.action) bits.push(`动作=${actionLabel(filters.action)}`)
  if (filters.status === 'ok') bits.push('仅成功')
  if (filters.status === 'denied') bits.push('仅被拒')
  if (since.value) bits.push(`起 ${since.value.replace('T', ' ')}`)
  if (until.value) bits.push(`止 ${until.value.replace('T', ' ')}`)
  if (filters.q.trim()) bits.push(`关键字=${filters.q.trim()}`)
  return bits.join(' · ')
})

async function loadActions() {
  try {
    const { data } = await systemApi.auditActions()
    actionOptions.value = data
  } catch {
    // 拉不到选项不影响主流程 —— 下拉就是空的，其他筛选照常能用。
    actionOptions.value = []
  }
}

/** 本页的 id 集合 —— 翻页/重查后用它把 `expanded` 里上一页的残留滤掉。 */
function pruneExpanded() {
  const alive = new Set(rows.value.map((r) => r.id))
  const next = new Set()
  for (const id of expanded.value) if (alive.has(id)) next.add(id)
  expanded.value = next
  // `autoExpanded` 也一样清 —— 它记的是"本页这些行自动展开过"，
  // 留着上一页的 id 只会让它无限膨胀。
  const seen = new Set()
  for (const id of autoExpanded.value) if (alive.has(id)) seen.add(id)
  autoExpanded.value = seen
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await systemApi.audit(buildParams())
    // ★ 后端现在给的是**信封**（{items,total,page,page_size,pages}），不是裸数组。
    rows.value = data.items || []
    pager.total = data.total ?? 0
    // ★ 用后端回的 page，不要用我们请求时那个：页码越界时后端会夹回最后一页，
    //   以它为准才不会出现"界面显示第 9 页、内容其实是第 7 页"。
    pager.page = data.page ?? 1
    pager.pageSize = data.page_size ?? pager.pageSize
    pager.pages = data.pages ?? 1
    jumpTo.value = pager.page
    editingId.value = null
    pruneExpanded()
    // ★ 必须在 prune 之后调：要先用本页的 id 把上一页的残留清掉，
    //   否则"这一页被拒的行"会被上一页的痕迹误判成"已经自动展开过"。
    autoExpandDenied()
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
    rows.value = []
    pager.total = 0
    pager.pages = 1
    // 加载失败时一并清掉展开痕迹 —— 留着只会让下一次加载误以为"这些行已经展开过"。
    expanded.value = new Set()
    autoExpanded.value = new Set()
  } finally {
    loading.value = false
  }
}

/** 换页 / 换每页条数。改动后**回第 1 页**再拉 —— 换了页大小还停在第 5 页
 *  很容易直接越界，而且用户的心理预期就是"重看开头"。 */
function onPageChange(p) {
  pager.page = p
  load()
}

function onSizeChange(s) {
  pager.pageSize = s
  pager.page = 1
  load()
}

/** 「跳转」按钮：把输入框里的页码夹到合法范围再跳。 */
function doJump() {
  const n = Number(jumpTo.value)
  if (!Number.isFinite(n)) {
    jumpTo.value = pager.page
    return
  }
  const p = Math.min(Math.max(1, Math.trunc(n)), pager.pages)
  if (p === pager.page) {
    jumpTo.value = pager.page
    return
  }
  onPageChange(p)
}

function reset() {
  filters.target = ''
  filters.actor = ''
  filters.action = ''
  filters.status = ''
  filters.q = ''
  since.value = ''
  until.value = ''
  pager.page = 1
  expanded.value = new Set()
  // 重置时把"自动展开过的痕迹"也清掉 —— 否则重置后同一批被拒记录
  // 会被当成"已经展开过"，不再自动撑开，用户会以为重置把默认展开弄丢了。
  autoExpanded.value = new Set()
  load()
}

/** 快捷时间窗：最近 N 小时 / 今天 / 最近 N 天。 */
function quickRange(kind) {
  const p = (n) => String(n).padStart(2, '0')
  const fmt = (d) =>
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
  const now = new Date()
  if (kind === 'today') {
    const s = new Date(now)
    s.setHours(0, 0, 0, 0)
    since.value = fmt(s)
    until.value = ''
  } else {
    const hours = kind === '1h' ? 1 : kind === '24h' ? 24 : 24 * 7
    const s = new Date(now.getTime() - hours * 3600 * 1000)
    since.value = fmt(s)
    until.value = ''
  }
  pager.page = 1
  load()
}

/** 打开备注编辑框（草稿默认取已有的备注）。同时把这条**展开** ——
 *  不然输入框落在一个收起的行里，用户看不见自己在编辑什么。 */
function openRemark(r) {
  drafts[r.id] = r.remark || ''
  editingId.value = r.id
  if (!expanded.value.has(r.id)) toggle(r)
}

function cancelRemark() {
  editingId.value = null
}

/** 保存备注；内容为空就是清空（后端把空串当清空处理，并会留一条痕）。 */
async function saveRemark(r) {
  const text = (drafts[r.id] || '').trim()
  savingId.value = r.id
  try {
    const { data } = await systemApi.setAuditRemark(r.id, text)
    // ★ 就地更新这一条，不整页重拉：重拉会把用户滚动位置和展开状态冲掉。
    const i = rows.value.findIndex((x) => x.id === r.id)
    if (i >= 0) rows.value[i] = { ...rows.value[i], ...data }
    ElMessage.success(text ? '备注已保存' : '备注已清空')
    editingId.value = null
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '保存失败')
  } finally {
    savingId.value = null
  }
}

onMounted(() => {
  loadActions()
  load()
})
</script>

<template>
  <div>
    <PageHeader title="审计流水" subtitle="只记元数据，不含明文、密钥、密文" />

    <!-- 第一行：原来的两个精确匹配 + 动作 + 状态（保持搜索栏的位置与手感） -->
    <div class="panel filter">
      <el-input v-model="filters.target" placeholder="按对象回放（所有者/文件标识，精确匹配）" clearable style="width: 300px" @keyup.enter="load" />
      <el-input v-model="filters.actor" placeholder="按操作者回放（精确匹配）" clearable style="width: 240px" @keyup.enter="load" />
      <el-select v-model="filters.action" placeholder="动作" clearable style="width: 150px" @change="load">
        <el-option
          v-for="o in actionOptions"
          :key="o.action"
          :label="actionLabel(o.action)"
          :value="o.action"
        >
          <span>{{ actionLabel(o.action) }}</span>
          <span class="opt-count">{{ o.count }}</span>
        </el-option>
      </el-select>
      <el-radio-group v-model="filters.status" @change="load">
        <el-radio-button value="">全部</el-radio-button>
        <el-radio-button value="ok">成功</el-radio-button>
        <el-radio-button value="denied">被拒</el-radio-button>
      </el-radio-group>
      <el-button type="primary" @click="load">查询</el-button>
      <el-button @click="reset">重置</el-button>
    </div>

    <!-- 第二行：时间窗 + 关键字。单独一行，免得挤在一条里换行凌乱。 -->
    <div class="panel filter">
      <el-date-picker
        v-model="since"
        type="datetime"
        placeholder="起始时间"
        value-format="YYYY-MM-DDTHH:mm:ss"
        :clearable="true"
        style="width: 190px"
        @change="load"
      />
      <span class="range-sep">~</span>
      <el-date-picker
        v-model="until"
        type="datetime"
        placeholder="截止时间（含当天）"
        value-format="YYYY-MM-DDTHH:mm:ss"
        :clearable="true"
        style="width: 190px"
        @change="load"
      />
      <el-button-group>
        <el-button size="small" @click="quickRange('1h')">近 1 小时</el-button>
        <el-button size="small" @click="quickRange('24h')">近 24 小时</el-button>
        <el-button size="small" @click="quickRange('7d')">近 7 天</el-button>
        <el-button size="small" @click="quickRange('today')">今天</el-button>
      </el-button-group>
      <el-input v-model="filters.q" placeholder="关键字（人 / 动作 / 对象 / 说明 / 备注）" clearable style="width: 260px" @keyup.enter="load" />
      <el-button @click="load">查询</el-button>
    </div>

    <p class="replay-note">
      查询类动作（query / verify / por）只能按操作者查：它们按全局下标记录，不挂在某个文件上。
      时间按<b>本地时间</b>填，后端换算 —— 起止为左闭右开（到点整不含）。
    </p>

    <div v-if="error" class="panel"><p class="text-danger">{{ error }}</p></div>
    <div v-else class="panel">
      <div class="result-bar" v-if="pager.total">
        <span class="result-count">共 <b>{{ pager.total }}</b> 条</span>
        <span class="result-page">第 {{ pager.page }} / {{ pager.pages }} 页</span>
        <span class="result-filter" v-if="hasFilter">筛选：{{ filterSummary }}</span>
        <el-button
          v-if="rows.some(hasMore)"
          link
          type="primary"
          size="small"
          class="expand-all-btn"
          @click="toggleAll"
        >
          {{ allExpanded ? '全部收起' : '全部展开' }}
        </el-button>
      </div>

      <el-timeline v-if="rows.length" v-loading="loading">
        <el-timeline-item
          v-for="r in rows"
          :key="r.id"
          :class="['audit-tl-item', { denied: !r.ok }]"
          :type="r.ok ? 'success' : 'danger'"
          :hollow="!r.ok"
        >
          <div class="audit-row" :class="{ denied: !r.ok }">
            <!-- ★ 折叠：摘要行**永远**显示；detail / 备注 / 编辑框在展开时才出现。
                 点整行任意空白处都能切换（不只有点那个小箭头才行），
                 因为这一行的可点区域本来就应该是整条。 -->
            <div
              class="line1"
              :class="{ clickable: hasMore(r) }"
              @click="hasMore(r) && toggle(r)"
            >
              <span class="caret" v-if="hasMore(r)">{{ expandedSet.has(r.id) ? '▾' : '▸' }}</span>
              <span class="caret caret-ghost" v-else>·</span>
              <span class="mono time">{{ fmtTime(r.ts) }}</span>
              <span class="actor mono">{{ r.actor }}</span>
              <span class="action" :title="r.action">{{ actionLabel(r.action) }}</span>
              <span class="target mono" v-if="r.target">{{ r.target }}</span>
              <el-tag v-if="!r.ok" type="danger" size="small" effect="dark" class="denied-tag">
                被拒
              </el-tag>
              <el-tag v-else type="success" size="small">成功</el-tag>
              <!-- 收起时给一个「有备注」的小标 —— 否则备注藏在折叠里就看不见了 -->
              <span class="remark-dot" v-if="r.remark" title="这条有人工备注">备注</span>
              <el-button
                link
                type="primary"
                size="small"
                class="remark-btn"
                :disabled="editingId === r.id"
                @click.stop="openRemark(r)"
              >
                {{ r.remark ? '改备注' : '加备注' }}
              </el-button>
            </div>

            <!-- —— 以下都是**展开**才渲染的 —— -->
            <template v-if="expandedSet.has(r.id)">
              <!-- 拒绝原因：被拒时加个前缀标签并加重字色，因为它才是这条记录的重点。 -->
              <div class="detail" :class="{ 'detail-denied': !r.ok }" v-if="r.detail">
                <span class="detail-label" v-if="!r.ok">拒绝原因</span>{{ r.detail }}
              </div>

              <!-- 备注（人工批注）—— 与上面的 detail 分开一块，样式也不同，
                   一眼能看出哪句是系统记的、哪句是人补的。 -->
              <div class="remark" v-if="r.remark && editingId !== r.id">
                <span class="remark-label">备注</span>
                <span class="remark-text">{{ r.remark }}</span>
                <span class="remark-meta text-3">
                  {{ r.remark_by }} · {{ fmtTime(r.remark_at) }}
                </span>
              </div>

              <div class="remark-edit" v-if="editingId === r.id">
                <el-input
                  v-model="drafts[r.id]"
                  type="textarea"
                  :rows="2"
                  maxlength="2000"
                  show-word-limit
                  placeholder="写点什么…（清空并保存 = 删除备注）"
                />
                <div class="remark-actions">
                  <el-button
                    type="primary"
                    size="small"
                    :loading="savingId === r.id"
                    @click="saveRemark(r)"
                  >
                    保存
                  </el-button>
                  <el-button size="small" @click="cancelRemark">取消</el-button>
                </div>
              </div>
            </template>
          </div>
        </el-timeline-item>
      </el-timeline>
      <EmptyState v-else-if="!loading" :title="hasFilter ? '没有符合条件的记录' : '暂无审计记录'" />

      <!-- 分页条：页码 / 每页条数 / 跳转。
           总数 ≤ 一页时整条藏起来 —— 只有 3 条还要翻页是噪音。 -->
      <div class="pager" v-if="pager.total > 0 && pager.pages > 1">
        <el-pagination
          layout="prev, pager, next"
          :current-page="pager.page"
          :page-size="pager.pageSize"
          :total="pager.total"
          :pager-count="7"
          background
          @current-change="onPageChange"
        />
        <div class="jump">
          <span>每页</span>
          <el-select
            :model-value="pager.pageSize"
            size="small"
            style="width: 78px"
            @change="onSizeChange"
          >
            <el-option :value="10" label="10 条" />
            <el-option :value="20" label="20 条" />
            <el-option :value="50" label="50 条" />
          </el-select>
          <span>跳到</span>
          <el-input
            v-model="jumpTo"
            size="small"
            style="width: 62px"
            @keyup.enter="doJump"
          />
          <span>页</span>
          <el-button size="small" @click="doJump">跳转</el-button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* 筛选区：两行**分开放**（第一行精确匹配 + 动作 + 状态，第二行时间 + 关键字）。
   挤在一行里的话，el-date-picker 那两个宽控件会把整条挤爆、换行后变得很难懂
   "哪个框属于哪一组"。分开就一眼看得出两组各管什么。 */
.filter {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 8px;
}
.filter:last-of-type {
  margin-bottom: 4px;
}
.range-sep {
  color: var(--text-3);
  font-size: 12px;
  margin: 0 -4px;
}
/* 动作下拉里右侧的出现次数：哑色、贴右，只用来说明"这个值有多少条"。 */
.opt-count {
  float: right;
  color: var(--text-3);
  font-size: 12px;
}
/* 结果头：共 N 条 + 第 x/y 页 + 筛的是什么 + 全部展开。
   分页之后「共 N 条」是**筛选后的总数**（不是本页条数）—— 这一点必须让
   使用者一眼能分辨，否则会把"本页 10 条"误读成"总共只有 10 条"。 */
.result-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--line);
  font-size: 12px;
  color: var(--text-3);
}
.result-count b {
  color: var(--text-1);
}
.result-page {
  color: var(--text-2);
}
.result-filter {
  color: var(--text-2);
}
.expand-all-btn {
  margin-left: auto;
}
/* 折叠箭头：固定宽度，展开/收起时后面的列不会左右跳动。 */
.caret {
  display: inline-block;
  width: 12px;
  color: var(--text-3);
  font-size: 11px;
  user-select: none;
}
.caret-ghost {
  color: transparent;
}
.line1.clickable {
  cursor: pointer;
}
.line1.clickable:hover .caret {
  color: var(--accent);
}
/* 「有备注」小标：收起时备注藏在折叠里，靠它提示"这条下面还有东西"。 */
.remark-dot {
  padding: 0 5px;
  font-size: 11px;
  color: var(--accent);
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  border-radius: 3px;
}
/* 分页条：页码 + 每页条数 + 跳转。靠右收尾并留一点上边距。 */
.pager {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  margin-top: 14px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}
.jump {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-3);
}
.replay-note {
  font-size: 12px;
  color: var(--text-3);
  margin-bottom: 12px;
}
.audit-row {
  font-size: 13px;
  /* 给每一条留出统一的左边距，被拒时才补上那道红边 —— 这样即使被拒记录
     有背景色块，内容也不会因为多了边框而整体右移、与成功记录错位。 */
  padding: 4px 10px 4px 0;
  border-left: 3px solid transparent;
  border-radius: 4px;
}
/* ★ 被拒的流水：整块淡红底 + 左侧粗红线 + 一点外发光。
   审计流水一屏几十条，"被拒"必须能在**扫视**时就跳出来 ——
   只把字变红不够，因为红字在白底上混在一片灰字里仍然要逐行读。
   色块能在不读文字的前提下先被眼睛抓住。 */
.audit-row.denied {
  padding-left: 12px;
  border-left-color: var(--danger);
  background: linear-gradient(
    90deg,
    color-mix(in srgb, var(--danger) 12%, transparent),
    transparent 70%
  );
}
html[data-theme='dark'] .audit-row.denied {
  background: linear-gradient(
    90deg,
    color-mix(in srgb, var(--danger) 20%, transparent),
    transparent 70%
  );
}
.audit-row.denied .action,
.audit-row.denied .target {
  color: var(--danger);
  font-weight: 500;
}
.audit-row.denied .time,
.audit-row.denied .actor {
  color: var(--danger);
  opacity: 0.75;
}
/* 被拒那行的折叠箭头也走红色 —— 灰箭头落在红底上对比度太低，
   而"这条能不能点开看拒绝原因"恰恰是被拒记录最该被看到的一件事。 */
.audit-row.denied .caret {
  color: color-mix(in srgb, var(--danger) 70%, transparent);
}
.audit-row.denied .line1.clickable:hover .caret {
  color: var(--danger);
}
.audit-row.denied .denied-tag {
  box-shadow: 0 0 0 3px color-mix(in srgb, var(--danger) 18%, transparent);
}
.line1 {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.time {
  color: var(--text-3);
  font-size: 12px;
}
.actor {
  color: var(--accent);
}
.action {
  font-weight: 500;
  color: var(--text-1);
}
.target {
  color: var(--text-2);
}
.detail {
  margin-top: 4px;
  color: var(--text-2);
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
}
/* 拒绝原因：字色加重、前面挂个红标签 —— 这是被拒记录里最该被读到的一句。 */
.detail-denied {
  color: var(--danger);
}
.detail-label {
  display: inline-block;
  margin-right: 6px;
  padding: 0 5px;
  font-size: 11px;
  color: #fff;
  background: var(--danger);
  border-radius: 3px;
  vertical-align: 1px;
}
/* 「加备注」按钮：默认半透明，鼠标移到这一条才亮出来 ——
   一屏几十个按钮全亮着太吵，但也不能藏起来找不到。 */
.remark-btn {
  opacity: 0.35;
  transition: opacity 0.15s ease;
}
.audit-row:hover .remark-btn,
.remark-btn:disabled {
  opacity: 1;
}
/* 被拒那条：备注按钮常亮 —— 既然已经被拒了，多半是要写点什么说明的。 */
.audit-row.denied .remark-btn {
  opacity: 0.8;
}

/* 时间轴的圆点：被拒的点放大并加一圈光晕，在竖直扫视时也是第一个被抓到的。 */
.audit-tl-item.denied :deep(.el-timeline-item__node) {
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--danger) 22%, transparent);
}
.audit-tl-item.denied :deep(.el-timeline-item__tail) {
  border-left-color: color-mix(in srgb, var(--danger) 40%, var(--line));
}
/* 人工备注：与 detail 视觉上分开 —— 左边一道强调色竖线 + 淡底。
   目的是让人一眼分清「系统记的」与「人补的」。 */
.remark {
  margin-top: 4px;
  padding: 5px 10px;
  font-size: 12px;
  color: var(--text-1);
  background: var(--bg-soft, rgba(0, 0, 0, 0.03));
  border-left: 2px solid var(--accent);
  border-radius: 3px;
}
.remark-label {
  display: inline-block;
  margin-right: 6px;
  padding: 0 5px;
  font-size: 11px;
  color: #fff;
  background: var(--accent);
  border-radius: 3px;
  vertical-align: 1px;
}
.remark-text {
  white-space: pre-wrap;
  word-break: break-all;
}
.remark-meta {
  margin-left: 8px;
  font-size: 11px;
}
.remark-edit {
  margin-top: 6px;
}
.remark-actions {
  margin-top: 6px;
  display: flex;
  gap: 8px;
}
</style>
