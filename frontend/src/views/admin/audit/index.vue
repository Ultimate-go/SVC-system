<script setup>
/**
 * 审计流水。
 *
 * - GET /api/admin/audit 为服务端分页，返回 {items, total, page, page_size, pages}。
 *   默认每页 10 条，可选 10/20/50；支持页码与跳转到指定页。
 *   过滤条件：
 *     actor    精确匹配操作者；查询类动作只能按此过滤（它们不挂在具体文件上）
 *     action   精确匹配动作名（下拉选项取自库中出现过的值）
 *     ok       仅成功 / 仅被拒
 *     since/until  时间窗（本地时间，半开区间）；换算为 UTC 由后端负责
 *     q        模糊匹配（actor / action / target / detail / remark）
 * - 折叠：默认只显示「时间 / 人 / 动作 / 对象 / 状态」摘要行；
 *   detail、备注与备注编辑框需展开后出现。顶部提供「全部展开 / 收起」。
 * - 被拒动作（ok: false）必须醒目标出。
 * - 备注：管理员可为任意一条流水写人工批注（PATCH /audit/{id}/remark）。
 *   备注与 detail 分开显示：detail 是系统当时记录的事实（如「不是所有者」），
 *   备注是人工解释（如「演示用途，非故障」）。两者混排将无法区分来源。
 * - 只记录元数据，不含明文、密钥与密文。
 *
 * 时间过滤：本地值原样提交给后端，不在前端换算为 UTC。
 *   库中存储的是 UTC（见 backend/models.py:utcnow）。前端换算需自行处理
 *   时区偏移；而本项目曾出现 8 小时显示偏差（format.js:fmtTime），
 *   原因正是把无时区后缀的 UTC 串当作本地时间。换算只应有一处实现。
 *   因此这里以 ``value-format="YYYY-MM-DDTHH:mm:ss"`` 输出本地时间并原样提交。
 *
 * 展开状态使用 ``Set<id>``，不使用下标，也不写入行对象：
 *   · 使用下标：翻页后下标会错位（每页都有第 3 行）；
 *   · 写入行对象（``r._expanded = true``）：重查或翻页会替换整个 rows，
 *     状态随旧数据丢失，换页返回后展开状态不再保留。
 *   以 id 为键，语义才是「这一条流水」而非「这一页的这一格」。
 *   换页后仍需 prune，避免集合持续增长。
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

//: 每行是否展开。以 `Set<id>` 为键而非下标：翻页后下标会错位
//: （第 1 页第 3 行与第 2 页第 3 行的下标都是 2，无法区分）。
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
 *  仅对首次出现的行生效：已在 `autoExpanded` 中的 id 不再改动，
 *    否则用户手动收起后，load() 会再次将其展开。 */
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
  // 必须换新实例再赋回：Set 原地 add 不触发 Vue 响应式。
  if (exp) expanded.value = exp
  if (seen) autoExpanded.value = seen
}

//: 折叠状态下**摘要行**要不要显示 detail 的前一段？
//: 这里刻意不显示：一屏 10 条，每条都拖一行小字，扫视时反而看不清状态色块。
//: 摘要行只给"时间 / 人 / 动作 / 对象 / 状态"。
//: 例外：**被拒**的记录默认整行展开（拒绝原因才是重点，见 autoExpandDenied）。
function toggle(r) {
  const s = expanded.value
  // 必须换一个新 Set 再赋回：直接 s.add/delete 不会触发 Vue 响应式
  //   （Set 的增删不被 Proxy 拦截，浅层 ref 无法追踪）。
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

/** 收集非空筛选项为请求参数。空串、空数组与未选项一律不发送。 */
function buildParams() {
  const params = { page: pager.page, page_size: pager.pageSize }
  const a = filters.actor.trim()
  const act = filters.action
  const q = filters.q.trim()
  if (a) params.actor = a
  if (act) params.action = act
  if (filters.status === 'ok') params.ok = true
  else if (filters.status === 'denied') params.ok = false
  if (since.value) params.since = since.value
  if (until.value) params.until = until.value
  if (q) params.q = q
  return params
}

/** 当前是否存在筛选条件；用于「重置」按钮高亮与空态文案。 */
const hasFilter = computed(
  () =>
    !!(filters.actor.trim() || filters.action ||
       filters.status || filters.q.trim() || since.value || until.value),
)

/** 当前筛选条件的摘要文本，显示在结果数旁。 */
const filterSummary = computed(() => {
  const bits = []
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
    // 选项拉取失败不影响主流程：下拉为空，其它筛选仍可用。
    actionOptions.value = []
  }
}

/** 本页的 id 集合；翻页或重查后用于清除 `expanded` 中上一页的残留。 */
function pruneExpanded() {
  const alive = new Set(rows.value.map((r) => r.id))
  const next = new Set()
  for (const id of expanded.value) if (alive.has(id)) next.add(id)
  expanded.value = next
  // autoExpanded 同样清除：它记录的是「本页这些行自动展开过」，
  // 保留上一页的 id 只会使其持续增长。
  const seen = new Set()
  for (const id of autoExpanded.value) if (alive.has(id)) seen.add(id)
  autoExpanded.value = seen
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await systemApi.audit(buildParams())
    // 后端返回信封结构 {items, total, page, page_size, pages}，不是裸数组。
    rows.value = data.items || []
    pager.total = data.total ?? 0
    // 使用后端返回的 page，而非请求时的页码：越界时后端会夹回最后一页，
    //   以后端返回值为准，否则会出现页码与内容不一致。
    pager.page = data.page ?? 1
    pager.pageSize = data.page_size ?? pager.pageSize
    pager.pages = data.pages ?? 1
    jumpTo.value = pager.page
    editingId.value = null
    pruneExpanded()
    // 必须在 prune 之后调用：先用本页 id 清除上一页残留，
    //   否则本页被拒的行会被上一页的痕迹误判为已自动展开。
    autoExpandDenied()
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
    rows.value = []
    pager.total = 0
    pager.pages = 1
    // 加载失败时一并清除展开痕迹，否则下次加载会误判这些行已展开。
    expanded.value = new Set()
    autoExpanded.value = new Set()
  } finally {
    loading.value = false
  }
}

/** 换页或换每页条数；改动后回到第 1 页再取数据。
 *  换页大小后仍停留在第 5 页容易越界，且用户预期是从头查看。 */
function onPageChange(p) {
  pager.page = p
  load()
}

function onSizeChange(s) {
  pager.pageSize = s
  pager.page = 1
  load()
}

/** 「跳转」按钮：将输入框页码夹到合法范围后跳转。 */
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
  filters.actor = ''
  filters.action = ''
  filters.status = ''
  filters.q = ''
  since.value = ''
  until.value = ''
  pager.page = 1
  expanded.value = new Set()
  // 重置时一并清除自动展开痕迹，否则重置后同一批被拒记录
  // 会被视为已展开而不再展开，表现为重置后默认展开失效。
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

/** 打开备注编辑框（草稿取已有备注），同时展开该行，
 *  否则输入框位于收起行内，用户无法看到编辑对象。 */
function openRemark(r) {
  drafts[r.id] = r.remark || ''
  editingId.value = r.id
  if (!expanded.value.has(r.id)) toggle(r)
}

function cancelRemark() {
  editingId.value = null
}

/** 保存备注；内容为空即清空（后端将空串视为清空并记录一条痕）。 */
async function saveRemark(r) {
  const text = (drafts[r.id] || '').trim()
  savingId.value = r.id
  try {
    const { data } = await systemApi.setAuditRemark(r.id, text)
    // 就地更新该条，不整页重新拉取，以免丢失滚动位置与展开状态。
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
    <PageHeader title="审计流水"  />

    <!-- 第一行：操作者 + 动作 + 状态 -->
    <div class="panel filter">
      <el-input v-model="filters.actor" placeholder="操作者" clearable style="width: 240px" @keyup.enter="load" />
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

    <!-- 第二行：时间窗与关键字 -->
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
      <el-input v-model="filters.q" placeholder="关键字（人 / 动作 / 对象 / 说明 / 备注）" clearable style="width: 280px" @keyup.enter="load" />
      <el-button @click="load">查询</el-button>
    </div>



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
            <!-- 折叠：摘要行始终显示；detail、备注与编辑框在展开时出现。
                 点击整行空白处均可切换，不限于箭头。 -->
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
              <!-- 收起时显示「有备注」标记，否则备注在折叠内不可见 -->
              <span class="remark-dot" v-if="r.remark" title="存在人工备注">备注</span>
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

            <!-- 以下内容仅在展开时渲染 -->
            <template v-if="expandedSet.has(r.id)">
              <!-- 拒绝原因：被拒时加前缀标签并加深字色 -->
              <div class="detail" :class="{ 'detail-denied': !r.ok }" v-if="r.detail">
                <span class="detail-label" v-if="!r.ok">拒绝原因</span>{{ r.detail }}
              </div>

              <!-- 备注（人工批注）：与上方 detail 分开显示，样式不同，
                   以便区分系统记录与人工批注。 -->
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
                  placeholder="输入备注（清空并保存即删除）"
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

      <!-- 分页条：页码、每页条数与跳转。
           总数不超过一页时整条隐藏。 -->
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
/* 筛选区分为两行：第一行为精确匹配、动作与状态，第二行为时间与关键字。
   若并入一行，el-date-picker 的宽控件会挤压布局，换行后不易分辨
   各控件所属分组。 */
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
  font-size: 13px;
  margin: 0 -4px;
}
/* 动作下拉右侧的出现次数：低对比度并右对齐，用于说明该值的记录条数。 */
.opt-count {
  float: right;
  color: var(--text-3);
  font-size: 13px;
}
/* 结果头：共 N 条、第 x/y 页、筛选条件与全部展开按钮。
   分页后「共 N 条」为筛选后的总数而非本页条数，必须让使用者
   能够分辨，避免把本页条数误读为总数。 */
.result-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--line);
  font-size: 13px;
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
  font-size: 13px;
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
/* 「有备注」标记：收起时提示该条存在附加内容。 */
.remark-dot {
  padding: 0 5px;
  font-size: 13px;
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
  font-size: 13px;
  color: var(--text-3);
}
.replay-note {
  font-size: 13px;
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
/* 被拒流水：整块淡红底、左侧粗红线与轻微外发光。
   审计流水一屏数十条，被拒记录必须在扫视时即可识别：
   仅改变字色不足，红字在灰字中仍需逐行辨认。
   色块可在不阅读文字的情况下被识别。 */
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
/* 被拒行的折叠箭头同样使用红色：灰箭头在红底上对比度不足，
   而展开查看拒绝原因正是被拒记录最需要被触发的操作。 */
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
  font-size: 13px;
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
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-all;
}
/* 拒绝原因：字色加深并前置红色标签。 */
.detail-denied {
  color: var(--danger);
}
.detail-label {
  display: inline-block;
  margin-right: 6px;
  padding: 0 5px;
  font-size: 13px;
  color: #fff;
  background: var(--danger);
  border-radius: 3px;
  vertical-align: 1px;
}
/* 「加备注」按钮：默认半透明，悬停该条时显示，
   避免一屏多个按钮同时醒目，同时保持可发现。 */
.remark-btn {
  opacity: 0.35;
  transition: opacity 0.15s ease;
}
.audit-row:hover .remark-btn,
.remark-btn:disabled {
  opacity: 1;
}
/* 被拒行：备注按钮常亮。 */
.audit-row.denied .remark-btn {
  opacity: 0.8;
}

/* 时间轴圆点：被拒记录的点放大并加光晕，便于竖直扫视时识别。 */
.audit-tl-item.denied :deep(.el-timeline-item__node) {
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--danger) 22%, transparent);
}
.audit-tl-item.denied :deep(.el-timeline-item__tail) {
  border-left-color: color-mix(in srgb, var(--danger) 40%, var(--line));
}
/* 人工备注：与 detail 视觉上分开，左侧强调色竖线与淡底色。
   用于区分系统记录与人工批注。 */
.remark {
  margin-top: 4px;
  padding: 5px 10px;
  font-size: 13px;
  color: var(--text-1);
  background: var(--bg-soft, rgba(0, 0, 0, 0.03));
  border-left: 2px solid var(--accent);
  border-radius: 3px;
}
.remark-label {
  display: inline-block;
  margin-right: 6px;
  padding: 0 5px;
  font-size: 13px;
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
  font-size: 13px;
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
