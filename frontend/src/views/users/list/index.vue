<script setup>
/**
 * 用户列表页（用户点名要求）。
 *
 * - 查询区：关键词（用户名/显示名）+ 角色下拉 + 状态下拉 + 密钥对下拉 + 查询/重置。
 * - 后端 GET /api/admin/users 没有任何过滤/分页参数，返回全量。过滤与分页都在前端做。
 * - 自己那一行不给「停用」「删除」。
 * - 删除前弹确认框，把后端返回的 warning 原样显示。
 * - 列表只显示库级 has_key 与 disabled，不显示「本次会话」列（那会永远撒谎）。
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { usersApi } from '../../../api/users'
import { useAuthStore } from '../../../stores/auth'
import { fmtTime } from '../../../utils/format'
import PageHeader from '../../../components/common/PageHeader.vue'
import FilterBar from '../../../components/common/FilterBar.vue'
import EmptyState from '../../../components/common/EmptyState.vue'
import Icon from '../../../components/icons/Icon.vue'

const auth = useAuthStore()
const router = useRouter()

const loading = ref(false)
const error = ref('')
const allUsers = ref([])

const keyword = ref('')
const roleFilter = ref('')
const statusFilter = ref('')
const keyFilter = ref('')

const page = ref(1)
const PAGE_SIZE = 10

const myself = computed(() => auth.user?.username)

const filtered = computed(() => {
  let out = allUsers.value
  if (keyword.value) {
    const kw = keyword.value.toLowerCase()
    out = out.filter(
      (u) =>
        (u.username || '').toLowerCase().includes(kw) ||
        (u.display_name || '').toLowerCase().includes(kw),
    )
  }
  if (roleFilter.value) out = out.filter((u) => u.role === roleFilter.value)
  if (statusFilter.value === 'enabled') out = out.filter((u) => !u.disabled)
  if (statusFilter.value === 'disabled') out = out.filter((u) => u.disabled)
  if (keyFilter.value === 'has') out = out.filter((u) => u.has_key)
  if (keyFilter.value === 'none') out = out.filter((u) => !u.has_key)
  return out
})

const total = computed(() => filtered.value.length)
const adminCount = computed(() => allUsers.value.filter((u) => u.role === 'admin').length)
const paged = computed(() => {
  const start = (page.value - 1) * PAGE_SIZE
  return filtered.value.slice(start, start + PAGE_SIZE)
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await usersApi.list()
    allUsers.value = data
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function reset() {
  keyword.value = ''
  roleFilter.value = ''
  statusFilter.value = ''
  keyFilter.value = ''
  page.value = 1
}

async function toggleDisabled(u) {
  try {
    await usersApi.patch(u.id, { disabled: !u.disabled })
    ElMessage.success(u.disabled ? '已启用' : '已停用')
    await load()
  } catch {
    // 错误已由拦截器弹出
  }
}

async function removeUser(u) {
  // 删除前，先拿不到 warning —— 需要调 DELETE 才知道。这里先弹确认，再调，调完把 warning 展示。
  // 后端 DELETE 返回 warning 全文。为在删除前展示，先做一次确认（用已知信息），
  // 删除成功后把返回的 warning 原样弹出来。
  try {
    await ElMessageBox.confirm(
      `确定删除用户 ${u.username} 吗？他名下的文件将转为墓碑名，此后无人能解密。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const { data } = await usersApi.remove(u.id)
    // ★ 后端返回的 warning 原样展示，不自己再编。
    ElMessageBox.alert(data.warning || '已删除', '删除结果', { type: 'info' })
    await load()
  } catch {
    // 错误已由拦截器弹出
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="user-banner">
      <span class="banner-num mono">{{ allUsers.length }}</span>
      <span class="banner-text">位用户已登记</span>
      <span class="banner-en mono">REGISTERED USERS</span>
    </div>

    <PageHeader title="用户管理" subtitle="本地筛选 · 每页 10 条">
      <el-button type="primary" @click="router.push('/users/add')">
        <Icon name="plus" :size="14" style="margin-right: 6px" />添加用户
      </el-button>
    </PageHeader>

    <FilterBar v-model:keyword="keyword" placeholder="搜索用户名 / 显示名" @search="page = 1" @reset="reset">
      <el-select v-model="roleFilter" placeholder="角色" clearable style="width: 110px">
        <el-option label="管理员" value="admin" />
        <el-option label="普通用户" value="user" />
      </el-select>
      <el-select v-model="statusFilter" placeholder="状态" clearable style="width: 110px">
        <el-option label="启用" value="enabled" />
        <el-option label="停用" value="disabled" />
      </el-select>
      <el-select v-model="keyFilter" placeholder="密钥对" clearable style="width: 110px">
        <el-option label="有密钥对" value="has" />
        <el-option label="无密钥对" value="none" />
      </el-select>
    </FilterBar>

    <div v-if="error" class="panel"><p class="text-danger">{{ error }}</p></div>
    <el-table v-else :data="paged" v-loading="loading" class="panel-table" border>
      <el-table-column prop="username" label="用户名" width="140">
        <template #default="{ row }"><span class="mono">{{ row.username }}</span></template>
      </el-table-column>
      <el-table-column prop="display_name" label="显示名" width="120" />
      <el-table-column prop="role" label="角色" width="100">
        <template #default="{ row }">
          <el-tag :type="row.role === 'admin' ? 'warning' : 'info'" size="small">
            {{ row.role === 'admin' ? '管理员' : '普通用户' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="密钥对" width="100">
        <template #default="{ row }">
          <span :class="row.has_key ? 'text-ok' : 'text-danger'">
            {{ row.has_key ? '有' : '无' }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <span :class="row.disabled ? 'text-danger' : 'text-ok'">
            {{ row.disabled ? '停用' : '启用' }}
          </span>
        </template>
      </el-table-column>
      <el-table-column prop="files" label="文件数" width="90" align="center" />
      <el-table-column label="操作" min-width="220">
        <template #default="{ row }">
          <el-button link type="primary" @click="router.push(`/users/${row.id}`)">编辑</el-button>
          <template v-if="row.username !== myself">
            <el-button link :type="row.disabled ? 'success' : 'warning'" @click="toggleDisabled(row)">
              {{ row.disabled ? '启用' : '停用' }}
            </el-button>
            <el-button link type="danger" @click="removeUser(row)">删除</el-button>
          </template>
          <span v-else class="text-3" style="font-size: 12px">（自己）</span>
        </template>
      </el-table-column>
      <template #empty>
        <EmptyState title="没有匹配的用户" description="调整筛选条件试试" />
      </template>
    </el-table>

    <div class="foot">
      <span class="text-2" style="font-size: 12px">
        共 {{ total }} 位用户 · {{ adminCount }} 位管理员
      </span>
      <el-pagination
        v-model:current-page="page"
        :page-size="PAGE_SIZE"
        :total="total"
        layout="prev, pager, next"
        small
      />
    </div>
  </div>
</template>

<style scoped>
.user-banner {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 20px 24px;
  margin-bottom: 16px;
  background: var(--bg-panel);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: inset 0 1px 0 var(--glow-inset);
  position: relative;
  overflow: hidden;
}
.user-banner::before {
  content: '';
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 2px;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
}
.banner-num {
  font-size: 44px;
  font-weight: 500;
  line-height: 1;
  color: var(--accent);
  text-shadow: 0 0 24px rgba(34, 211, 238, 0.35);
  font-variant-numeric: tabular-nums;
}
.banner-text {
  font-size: 22px;
  font-weight: 500;
  color: var(--text-1);
}
.banner-en {
  margin-left: auto;
  font-size: 11px;
  letter-spacing: 0.18em;
  color: var(--text-3);
}
.panel-table {
  border-radius: var(--radius);
}
.foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 12px;
}
</style>
