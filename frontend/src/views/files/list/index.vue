<script setup>
/**
 * 文件与块。
 *
 * - 上传卡：选文件 + file_key + 切法三档（自动/按块大小/按块数）+ 存到哪几台。
 * - 上传前先问 /api/plan，把 why 与 alternatives 显示出来；ok:false 显著提示。
 * - 文件列表：owner / file_key / 块数 / 大小 / 版本 / 全局下标(span) / 两把锁 / 试解密。
 * - 「试解密」不置灰：点下去才看到后端真的拒了你（演示亮点）。
 */
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { filesApi } from '../../../api/files'
import { devicesApi } from '../../../api/devices'
import { systemApi } from '../../../api/system'
import { useAuthStore } from '../../../stores/auth'
import { span, fmtBytes, hexPreview } from '../../../utils/format'
import { SPLIT_MODES } from '../../../utils/constants'
import PageHeader from '../../../components/common/PageHeader.vue'
import LockTag from '../../../components/security/LockTag.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'
import EmptyState from '../../../components/common/EmptyState.vue'
import Icon from '../../../components/icons/Icon.vue'

const auth = useAuthStore()
const router = useRouter()

const loading = ref(false)
const error = ref('')
const files = ref([])
const nodes = ref([])
const status = ref(null)

// 上传表单
const fileRef = ref()
const uploadForm = reactive({
  fileKey: '',
  splitMode: 'auto',
  segmentBytes: 1024,
  blockCount: 4,
  pickedNodes: [],
})

const plan = ref(null)
const askingPlan = ref(false)
const uploading = ref(false)
const uploadElapsed = ref(0)
const uploadTimings = ref(null)
let elapsedTimer = null

const selectedFile = ref(null)

const canUpload = computed(() => selectedFile.value && uploadForm.fileKey.trim())

const maxBytes = computed(() => status.value?.segment_bytes_max || 1048576)

function onFileChange(f) {
  selectedFile.value = f?.raw || null
}

async function askPlan() {
  if (!selectedFile.value) return
  askingPlan.value = true
  plan.value = null
  try {
    const { data } = await devicesApi.plan(selectedFile.value.size, 'fewest_blocks')
    plan.value = data
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '顾问询问失败')
  } finally {
    askingPlan.value = false
  }
}

function buildFormData() {
  const fd = new FormData()
  fd.append('file', selectedFile.value)
  fd.append('file_key', uploadForm.fileKey.trim())
  // 切法 → segment_bytes
  let seg = null
  if (uploadForm.splitMode === 'auto') {
    seg = plan.value?.ok ? plan.value.segment_bytes : null
  } else if (uploadForm.splitMode === 'by_size') {
    seg = uploadForm.segmentBytes
  } else if (uploadForm.splitMode === 'by_count') {
    seg = Math.max(1, Math.ceil(selectedFile.value.size / uploadForm.blockCount))
  }
  if (seg != null) fd.append('segment_bytes', String(seg))
  if (uploadForm.pickedNodes.length) {
    fd.append('nodes', uploadForm.pickedNodes.join(','))
  }
  return fd
}

async function doUpload() {
  if (!canUpload) return
  uploading.value = true
  uploadTimings.value = null
  uploadElapsed.value = 0
  elapsedTimer = setInterval(() => (uploadElapsed.value += 100), 100)
  try {
    const { data } = await filesApi.upload(buildFormData())
    uploadTimings.value = data.timings
    ElMessage.success('上传完成')
    resetUpload()
    await load()
  } catch {
    // 错误已由拦截器弹出（含 503 写推失败、413 超限等）
  } finally {
    clearInterval(elapsedTimer)
    uploading.value = false
  }
}

function resetUpload() {
  selectedFile.value = null
  uploadForm.fileKey = ''
  plan.value = null
  if (fileRef.value) fileRef.value.clearFiles()
}

async function tryDecrypt(file) {
  // ★ 不置灰：点下去才看到后端真的拒了你。
  try {
    const { data } = await filesApi.decrypt(file.id, null)
    const len = data.bytes
    ElMessage.success(`解密成功：${len} 字节（${file.owner}/${file.file_key}）`)
  } catch {
    // 403 的中文理由已由拦截器原样弹出
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [f, n, s] = await Promise.all([filesApi.list(), devicesApi.nodes(), systemApi.status()])
    files.value = f.data
    nodes.value = n.data
    status.value = s.data
    // 默认全选节点
    if (!uploadForm.pickedNodes.length) {
      uploadForm.pickedNodes = n.data.filter((x) => !x.unreachable).map((x) => x.node_id)
    }
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <PageHeader title="文件与块" subtitle="所有人可验证，但仅所有者能解密" />

    <div v-if="error" class="panel"><p class="text-danger">{{ error }}</p></div>
    <template v-else>
      <div class="panel mb-3">
        <h4 class="sec-title">上传文件</h4>
        <div class="upload-row">
          <el-upload
            ref="fileRef"
            :auto-upload="false"
            :show-file-list="true"
            :limit="1"
            :on-change="onFileChange"
            drag
            class="upload-drag"
          >
            <Icon name="upload" :size="28" />
            <div class="el-upload__text">拖文件到这里，或点击选择</div>
          </el-upload>

          <div class="upload-form">
            <el-input v-model="uploadForm.fileKey" placeholder="请输入文件标识" />
            <el-radio-group v-model="uploadForm.splitMode">
              <el-radio-button v-for="m in SPLIT_MODES" :key="m.value" :value="m.value">{{ m.label }}</el-radio-button>
            </el-radio-group>
            <div v-if="uploadForm.splitMode === 'by_size'" class="mono">
              <span class="text-2">块大小（字节）：</span>
              <el-input-number v-model="uploadForm.segmentBytes" :min="status?.segment_bytes_min || 64" :max="status?.segment_bytes_max || 1048576" :step="64" />
            </div>
            <div v-else-if="uploadForm.splitMode === 'by_count'" class="mono">
              <span class="text-2">切成几块：</span>
              <el-input-number v-model="uploadForm.blockCount" :min="1" :max="1024" />
            </div>

            <div class="nodes-pick">
              <span class="text-2">存储位置：</span>
              <el-checkbox-group v-model="uploadForm.pickedNodes">
                <el-checkbox v-for="n in nodes" :key="n.node_id" :value="n.node_id" :disabled="n.unreachable">
                  <span class="mono">{{ n.node_id }}</span>
                </el-checkbox>
              </el-checkbox-group>
            </div>

            <div class="actions">
              <el-button :disabled="!selectedFile" :loading="askingPlan" @click="askPlan">寻求建议</el-button>
              <el-button type="primary" :disabled="!canUpload" :loading="uploading" @click="doUpload">上传</el-button>
              <span v-if="uploading" class="mono elapsed">已用 {{ (uploadElapsed / 1000).toFixed(1) }} s</span>
            </div>
          </div>
        </div>

        <div v-if="plan" class="plan-box">
          <template v-if="plan.ok">
            <div class="text-ok">建议切 {{ plan.segment_bytes }} 字节一块，共 {{ plan.blocks }} 块</div>
            <div class="text-2" style="font-size: 12px">{{ plan.why }}</div>
            <div v-if="plan.alternatives?.length" class="alt">
              <div v-for="a in plan.alternatives" :key="a.segment_bytes" class="alt-row">
                <span class="mono">{{ a.segment_bytes }} B → {{ a.blocks }} 块</span>
                <span class="text-3">{{ a.est_upload_ms }} ms 上传 / {{ a.est_verify_ms }} ms 验证</span>
                <span v-if="a.note" class="text-3">{{ a.note }}</span>
              </div>
            </div>
            <p class="text-3" style="font-size: 11px">口径：est_* 是本地那几步的估算，不含分发到节点那一段。</p>
          </template>
          <template v-else>
            <el-alert type="error" :closable="false" title="放不下" :description="plan.reason" />
          </template>
        </div>

        <StageTimeline v-if="uploadTimings" :timings="uploadTimings" class="mt-3" />
      </div>

      <div class="panel">
        <h4 class="sec-title">文件列表</h4>
        <el-table :data="files" v-loading="loading" border>
          <el-table-column prop="owner" label="所有者" width="110">
            <template #default="{ row }"><span class="mono">{{ row.owner }}</span></template>
          </el-table-column>
          <el-table-column label="文件标识" min-width="150">
            <template #default="{ row }">
              <el-link type="primary" @click="router.push(`/files/${row.id}`)">{{ row.file_key }}</el-link>
            </template>
          </el-table-column>
          <el-table-column prop="block_count" label="块数" width="70" align="center" />
          <el-table-column label="大小" width="90">
            <template #default="{ row }"><span class="mono">{{ fmtBytes(row.total_bytes) }}</span></template>
          </el-table-column>
          <el-table-column label="版本" width="70" align="center">
            <template #default="{ row }"><span class="mono">{{ row.version }}</span></template>
          </el-table-column>
          <el-table-column label="全局下标" min-width="120">
            <template #default="{ row }"><span class="mono">{{ span(row.indices) }}</span></template>
          </el-table-column>
          <el-table-column label="验证" width="80" align="center">
            <template #default>
              <span class="text-accent" title="验证不受限，所有人都能验证">可验证</span>
            </template>
          </el-table-column>
          <el-table-column label="解密" width="90" align="center">
            <template #default="{ row }">
              <LockTag :can-decrypt="row.can_decrypt" />
            </template>
          </el-table-column>
          <el-table-column label="操作" width="90" align="center">
            <template #default="{ row }">
              <el-button link type="primary" @click="tryDecrypt(row)">试解密</el-button>
            </template>
          </el-table-column>
          <template #empty>
            <EmptyState title="还没有文件" description="上传一个试试，切成几块、每块多大都由你决定" />
          </template>
        </el-table>
      </div>
    </template>
  </div>
</template>

<style scoped>
.sec-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 14px;
}
.upload-row {
  display: flex;
  gap: 20px;
  flex-wrap: wrap;
}
.upload-drag {
  width: 260px;
}
.upload-form {
  flex: 1;
  min-width: 300px;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.nodes-pick {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
.elapsed {
  font-size: 12px;
  color: var(--accent);
}
.plan-box {
  margin-top: 14px;
  padding: 12px;
  background: var(--bg-raised);
  border-radius: var(--radius-sm);
  border: 1px solid var(--line);
  font-size: 13px;
}
.alt {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.alt-row {
  display: flex;
  gap: 16px;
  font-size: 12px;
}
</style>
