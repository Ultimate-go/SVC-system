<script setup>
/**
 * 审计流水。
 *
 * - GET /api/admin/audit?limit=&target=&actor=，最新在前。
 * - 被拒的动作（ok:false）要显眼标出。
 * - 回放：target（所有者/文件标识）或 actor 两个精确匹配过滤。
 * - 只记元数据，永远不含明文/密钥/密文。
 */
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { systemApi } from '../../../api/system'
import { fmtTime } from '../../../utils/format'
import PageHeader from '../../../components/common/PageHeader.vue'
import EmptyState from '../../../components/common/EmptyState.vue'

const loading = ref(false)
const error = ref('')
const rows = ref([])

const target = ref('')
const actor = ref('')

async function load() {
  loading.value = true
  error.value = ''
  try {
    const params = { limit: 300 }
    if (target.value.trim()) params.target = target.value.trim()
    if (actor.value.trim()) params.actor = actor.value.trim()
    const { data } = await systemApi.audit(params)
    rows.value = data
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function replay(field) {
  // 回放：只保留精确匹配那一边，清空另一边，重新加载。
  load()
}

onMounted(load)
</script>

<template>
  <div>
    <PageHeader title="审计流水" subtitle="只记元数据，永远不含明文 / 密钥 / 密文" />

    <div class="panel filter">
      <el-input v-model="target" placeholder="按对象回放（所有者/文件标识，精确匹配）" clearable style="width: 300px" @keyup.enter="load" />
      <el-input v-model="actor" placeholder="按操作者回放（精确匹配）" clearable style="width: 240px" @keyup.enter="load" />
      <el-button type="primary" @click="load">查询</el-button>
      <el-button @click="target = ''; actor = ''; load()">重置</el-button>
    </div>

    <p class="replay-note">
      查询类动作（query / verify / por）只能按操作者（actor）捞 —— 它们按全局下标记，不落在具体文件上。
    </p>

    <div v-if="error" class="panel"><p class="text-danger">{{ error }}</p></div>
    <div v-else class="panel">
      <el-timeline v-if="rows.length" v-loading="loading">
        <el-timeline-item
          v-for="r in rows"
          :key="r.id"
          :type="r.ok ? 'success' : 'danger'"
          :hollow="!r.ok"
        >
          <div class="audit-row" :class="{ denied: !r.ok }">
            <div class="line1">
              <span class="mono time">{{ fmtTime(r.ts) }}</span>
              <span class="actor mono">{{ r.actor }}</span>
              <span class="action">{{ r.action }}</span>
              <span class="target mono" v-if="r.target">{{ r.target }}</span>
              <el-tag :type="r.ok ? 'success' : 'danger'" size="small">
                {{ r.ok ? '成功' : '被拒' }}
              </el-tag>
            </div>
            <div class="detail" v-if="r.detail">{{ r.detail }}</div>
          </div>
        </el-timeline-item>
      </el-timeline>
      <EmptyState v-else-if="!loading" title="暂无审计记录" />
    </div>
  </div>
</template>

<style scoped>
.filter {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}
.replay-note {
  font-size: 12px;
  color: var(--text-3);
  margin-bottom: 12px;
}
.audit-row {
  font-size: 13px;
}
.audit-row.denied .action,
.audit-row.denied .target {
  color: var(--danger);
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
}
</style>
