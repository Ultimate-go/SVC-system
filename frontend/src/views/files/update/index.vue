<script setup>
/**
 * 更新文件（★ 2026-10-12 从「文件详情」里拆出来的入口）。
 *
 * 为什么拆：以前「文件详情」既放**读**的东西（摘要、块分布、块明细），
 * 又放**改**的东西（改块 / 追加 / 截断 / 回滚演示 / 解密预览）。
 * 一份文件的详情页因此长得像工具箱，看的人和改的人挤在同一页里。
 *
 * 现在分工是：
 *   · 「文件详情」= 只读：这份文件是什么、块在哪、指纹长什么样；
 *   · 「更新文件」= 改它：选中一份文件后直接进它的操作详情。
 *
 * 所以本页只做一件事：**把文件选出来**。列表比「上传文件」那页更轻 ——
 * 不放上传、不放删除，只留"这份文件现在什么样"与一个「进入更新」。
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { filesApi } from '../../../api/files'
import { span, fmtBytes } from '../../../utils/format'
import PageHeader from '../../../components/common/PageHeader.vue'

const router = useRouter()

const loading = ref(false)
const error = ref('')
const files = ref([])
// 默认只看自己的：改块 / 追加 / 截断都只有所有者能做，别人的文件进不去。
const onlyMine = ref(true)

const visible = computed(() => (onlyMine.value ? files.value.filter((f) => f.is_mine) : files.value))
const mineCount = computed(() => files.value.filter((f) => f.is_mine).length)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await filesApi.list()
    files.value = data || []
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

function enter(row) {
  router.push({ name: 'files-update-detail', params: { id: row.id } })
}

onMounted(load)
</script>

<template>
  <div>
    <PageHeader title="更新文件">
      <el-button :loading="loading" @click="load">刷新</el-button>
    </PageHeader>

    <div class="panel mb-3">
      <div class="head">
        <div>
          <p class="lead">选中一份文件，进入它的更新页：改块 / 清零 / 追加 / 截断，以及浏览器本地的解密验证。</p>
          <p class="sub">
            这些操作会改写内容并让版本号 +1，只有文件所有者能做。别人的文件在这里是灰的。
          </p>
        </div>
        <el-switch v-model="onlyMine" active-text="只看我的" />
      </div>
    </div>

    <div v-if="error" class="panel"><p class="text-danger">{{ error }}</p></div>

    <div v-else class="panel">
      <div class="panel-title-row">
        <span class="panel-subtitle">共 {{ files.length }} 份文件，其中我的 {{ mineCount }} 份</span>
        <span class="live-mark mono">{{ visible.length }} SHOWN</span>
      </div>

      <el-table v-loading="loading" :data="visible" size="small" border>
        <el-table-column label="文件" min-width="200">
          <template #default="{ row }">
            <span class="mono">{{ row.owner }} / {{ row.file_key }}</span>
            <el-tag v-if="row.is_mine" type="success" size="small" effect="plain" class="tag">我的</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="100" align="center">
          <template #default="{ row }"><span class="mono">{{ fmtBytes(row.total_bytes) }}</span></template>
        </el-table-column>
        <el-table-column label="块数" width="80" align="center">
          <template #default="{ row }"><span class="mono">{{ row.block_count }}</span></template>
        </el-table-column>
        <el-table-column label="版本" width="80" align="center">
          <template #default="{ row }"><span class="mono">v{{ row.version }}</span></template>
        </el-table-column>
        <el-table-column label="块编号（第几块）" min-width="160">
          <template #default="{ row }"><span class="mono">{{ span(row.indices) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="130" align="center">
          <template #default="{ row }">
            <el-button size="small" type="primary" :disabled="!row.is_mine" @click="enter(row)">进入更新</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <p class="text-3" style="padding: 12px 0">
            {{ files.length ? '没有属于你的文件 —— 关掉「只看我的」能看到别人的（但不能改）。' : '还没有文件。' }}
          </p>
        </template>
      </el-table>

      <details class="note-collapse">
        <summary>说明</summary>
        <p class="note">
          更新是「重新加密 + 重新承诺」：改块只换这一块（其余块不动），追加在末尾接新块，
          截断只删这份文件自己的末尾。三种都会让版本号 +1，并把这份文件新的 δ 记进本地。
        </p>
      </details>
    </div>
  </div>
</template>

<style scoped>
.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.lead { margin: 0 0 6px; color: var(--text-1); font-size: 14px; }
.sub { margin: 0; color: var(--text-3); font-size: 12px; line-height: 1.7; }
.panel-title-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.panel-subtitle { color: var(--text-2); font-size: 12px; }
.live-mark { color: var(--text-3); font-size: 10px; letter-spacing: .08em; }
.tag { margin-left: 8px; }
</style>
