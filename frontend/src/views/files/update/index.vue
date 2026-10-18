<script setup>
/**
 * 更新文件（★ 2026-10-12 从「文件详情」里拆出来的入口）。
 *
 * 为什么拆：以前「文件详情」既放**读**的东西（摘要、块分布、块明细），
 * 又放**改**的东西（替换 / 置零 / 追加 / 删除 / 回滚演示 / 解密预览）。
 * 一份文件的详情页因此长得像工具箱，看的人和改的人挤在同一页里。
 *
 * 现在分工是：
 *   · 「文件详情」= 只读：这份文件是什么、块在哪、指纹长什么样；
 *   · 「更新文件」= 更新它：选中一份文件后直接进它的操作详情。
 *
 * 所以本页只做一件事：**把文件选出来**。列表比「上传文件」那页更轻 ——
 * 不放上传，只留“这份文件现在什么样”、一个「进入更新」与一个「删除」。
 * （删除是 2026-10-18 加的：它也是“更新”的一部分，而且放在这里比让人
 *   先点进详情再删少一步。）
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { filesApi } from '../../../api/files'
import { forgetAnchorsOf } from '../../../utils/anchor'
import { span, fmtBytes } from '../../../utils/format'
import PageHeader from '../../../components/common/PageHeader.vue'

const router = useRouter()

const loading = ref(false)
const error = ref('')
const files = ref([])
// 默认只看自己的：替换 / 置零 / 追加 / 删除都只有所有者能做，别人的文件进不去。
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

/**
 * 删除整份文件（与「进入更新」并列，用户 2026-10-18）。
 *
 * ★ 只有所有者看得到这个按钮（后端也是这么判的），且必须二次确认 ——
 *   删的是密文 + 封装过的块密钥 + 账目，不可撤销。
 * ★ 顺手清掉**本浏览器给这份文件留的锚**：库会复用 id，不清的话下一份
 *   拿到同一个 id 的文件会被这份旧基线冤枉（见 utils/anchor.js）。
 */
async function removeFile(row) {
  try {
    await ElMessageBox.confirm(
      `将删除 ${row.owner}/${row.file_key}（${row.block_count} 块）。\n\n` +
        '删除不可撤销：密文、封装过的块密钥与账目都会从库里、从节点上删掉。\n' +
        '别的文件不受影响 —— 每份文件各占独立的位置段。',
      '确认删除',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  try {
    const { data } = await filesApi.remove(row.id)
    forgetAnchorsOf(row.id)
    ElMessage.success(`已删除 ${data.dropped_blocks} 块`)
    await load()
  } catch {
    // 失败原因已由拦截器弹出（只有所有者可删 / 文件不存在…）
  }
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
          <p class="lead">选择一份文件进入更新视图：替换、追加、删除，以及浏览器本地的解密与验证。</p>
          <p class="sub">
            上述三类操作均会改写密文，并使版本号递增 1；写权限仅限文件持有者，非持有者不可操作。
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
            <!-- ★ 停用**不删文件**：这份文件还在列表里、仍可验证，
                 但它的所有者已登不进来，“进入更新”对谁都是不可用的。 -->
            <el-tag
              v-if="row.owner_disabled"
              type="info"
              size="small"
              effect="plain"
              class="tag"
              title="所有者已被停用：文件仍可验证，但本人无法登录，也无法更新"
            >所有者已停用</el-tag>
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
        <el-table-column label="位置段（全局编号）" min-width="170">
          <template #default="{ row }"><span class="mono">{{ span(row.indices) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="210" align="center">
          <template #default="{ row }">
            <el-button size="small" type="primary" :disabled="!row.is_mine" @click="enter(row)">进入更新</el-button>
            <!-- ★ 与「进入更新」并列（用户 2026-10-18）：删整份文件也属于“更新”的一部分。
                 非所有者不显示 —— 后端同样只允许所有者删。 -->
            <el-button
              v-if="row.is_mine"
              size="small"
              type="danger"
              plain
              style="margin-left: 8px"
              @click="removeFile(row)"
            >删除</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <p class="text-3" style="padding: 12px 0">
            {{ files.length ? '当前账号名下没有文件。关闭「只看我的」可查看其他所有者的文件，但不具备写权限。' : '系统中尚无文件。' }}
          </p>
        </template>
      </el-table>

      <details class="note-collapse">
        <summary>说明</summary>
        <p class="note">
          替换的语义是「重新加密并重新计算承诺」：替换只改写指定分块的内容，其余分块保持不变；
          置零把指定分块替换为等长的全 0 字节；
          追加在向量末尾增加新分块；删除仅移除本文件所占位置段的末尾部分。
          四类操作都会使版本号递增 1，并<b>作废</b>本浏览器此前为该文件钉住的聚合承诺 δ ——
          新的 δ 不会自动被接受：要等到在本页把该文件解密并<b>本地验证通过</b>之后，才会重新记下。
        </p>
      </details>
    </div>
  </div>
</template>

<style scoped>
.head { display: flex; align-items: flex-start; justify-content: space-between; gap: 20px; }
.lead { margin: 0 0 6px; color: var(--text-1); font-size: 14px; }
.sub { margin: 0; color: var(--text-3); font-size: 13px; line-height: 1.7; }
.panel-title-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.panel-subtitle { color: var(--text-2); font-size: 13px; }
.live-mark { color: var(--text-3); font-size: 13px; letter-spacing: .08em; }
.tag { margin-left: 8px; }
</style>
