<script setup>
/**
 * 文件详情。
 *
 * - 文件元信息 + 版本 + delta_n / delta_fp。
 * - 块分布矩阵（layout[].replicas 标主/副本）。
 * - **块明细表**：每块的公开分量（指纹）—— 跟着「简略/详细」开关
 *   决定要不要铺出来；详细模式会把每块的分量一起要回来（`?elements=1`）。
 * - 改块 / 追加 / 截断（三个写操作都显示 timings）。
 * - 解密预览（data_hex → hexToText，用 hasBadBytes 区分残缺）。
 */
import { ref, reactive, computed, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { filesApi } from '../../../api/files'
import { devicesApi } from '../../../api/devices'
import { useAuthStore } from '../../../stores/auth'
import { useThemeStore } from '../../../stores/theme'
import { span, fmtBytes, hexFp, decodeBlockHex, hasBadBytes } from '../../../utils/format'
import PageHeader from '../../../components/common/PageHeader.vue'
import DetailToggle from '../../../components/common/DetailToggle.vue'
import HashText from '../../../components/common/HashText.vue'
import BlockMatrix from '../../../components/chart/BlockMatrix.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'
import LockTag from '../../../components/security/LockTag.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const theme = useThemeStore()

const id = computed(() => route.params.id)
const loading = ref(false)
const error = ref('')
const file = ref(null)
const nodes = ref([])

/**
 * 「简略 / 详细」—— 详细模式要多要一份数据：每块的分量（`?elements=1`）。
 *
 * ★ 所以它一变就要**重新拉一次详情**，而不是只切 `v-if`：简略模式那次
 *   响应里**根本没有** element 字段（后端按参数决定给不给），
 *   光切 v-if 会看到一整列空白。
 */
const detailed = computed(() => theme.detailMode === 'detail')
watch(detailed, () => load())

// 写操作表单
const activeOp = ref('modify')
const modifyForm = reactive({ blockIdx: 0, text: '' })
const appendText = ref('')
const dropBlocks = ref(1)

const writeRunning = ref(false)
const writeTimings = ref(null)

// 解密
const decrypting = ref(false)
const decryptResult = ref(null)

const isMine = computed(() => file.value?.is_mine)

const decodePreview = computed(() => {
  if (!decryptResult.value?.data_hex) return null
  return decodeBlockHex(decryptResult.value.data_hex)
})

function toB64(str) {
  // 浏览器端用 TextEncoder 转 base64（UTF-8 安全）。
  const bytes = new TextEncoder().encode(str)
  let bin = ''
  bytes.forEach((b) => (bin += String.fromCharCode(b)))
  return btoa(bin)
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [{ data: f }, { data: n }] = await Promise.all([
      filesApi.detail(id.value, detailed.value),
      devicesApi.nodes(),
    ])
    file.value = f
    nodes.value = n
    if (f.layout?.length) modifyForm.blockIdx = f.layout[0].block_idx
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function doModify() {
  const body = { op: 'modify', block_idx: modifyForm.blockIdx, data_b64: toB64(modifyForm.text) }
  writeRunning.value = true
  writeTimings.value = null
  try {
    const { data } = await filesApi.patch(id.value, body)
    writeTimings.value = data.timings
    ElMessage.success(`已改第 ${modifyForm.blockIdx} 块，现在是第 ${data.version} 版`)
    await load()
  } catch {
  } finally {
    writeRunning.value = false
  }
}

async function doAppend() {
  const body = { op: 'append', data_b64: toB64(appendText.value) }
  writeRunning.value = true
  writeTimings.value = null
  try {
    const { data } = await filesApi.patch(id.value, body)
    writeTimings.value = data.timings
    ElMessage.success(`已追加 ${data.added_blocks} 块，共 ${data.block_count} 块`)
    appendText.value = ''
    await load()
  } catch {
  } finally {
    writeRunning.value = false
  }
}

async function doTruncate() {
  try {
    await ElMessageBox.confirm(
      `确定删除末尾 ${dropBlocks.value} 块吗？这是真的删数据、不可撤销。`,
      '二次确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  const body = { op: 'truncate', drop_blocks: dropBlocks.value }
  writeRunning.value = true
  writeTimings.value = null
  try {
    const { data } = await filesApi.patch(id.value, body)
    writeTimings.value = data.timings
    ElMessage.success(`已删除末尾 ${data.dropped_blocks} 块`)
    await load()
  } catch {
  } finally {
    writeRunning.value = false
  }
}

async function doDecrypt() {
  decrypting.value = true
  decryptResult.value = null
  try {
    const { data } = await filesApi.decrypt(id.value, null)
    decryptResult.value = data
  } catch {
    // 403 已由拦截器弹出
  } finally {
    decrypting.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <PageHeader :title="file ? `${file.owner} / ${file.file_key}` : '文件详情'">
      <el-button @click="router.push('/files')">返回</el-button>
    </PageHeader>

    <div v-if="error" class="panel"><p class="text-danger">{{ error }}</p></div>
    <template v-else-if="file">
      <div class="panel mb-3">
        <div class="meta">
          <div class="meta-item"><span class="k">大小</span><span class="mono">{{ fmtBytes(file.total_bytes) }}</span></div>
          <div class="meta-item"><span class="k">块数</span><span class="mono">{{ file.block_count }}</span></div>
          <div class="meta-item"><span class="k">版本</span><span class="mono">{{ file.version }}</span></div>
          <div class="meta-item"><span class="k">全局下标</span><span class="mono">{{ span(file.indices) }}</span></div>
          <div class="meta-item"><span class="k">δ_n</span><span class="mono">{{ file.delta_n }}</span></div>
          <div class="meta-item"><span class="k">δ 指纹</span><span class="mono">{{ file.delta_fp }}</span></div>
          <div class="meta-item"><LockTag :can-decrypt="file.can_decrypt" /></div>
        </div>
        <div class="digest text-2" style="font-size: 12px">
          上传时摘要（SHA-256）：<span class="mono">{{ hexFp(file.content_digest, 16) }}</span>
          <span class="text-3">（它不随改块更新，协调者手里没有完整明文）</span>
        </div>
      </div>

      <div class="panel mb-3">
        <h4 class="sec-title">块分布矩阵</h4>
        <BlockMatrix :nodes="nodes" :layout="file.layout" />
      </div>

      <!-- 块明细：每一块的**公开分量**就是它的指纹。 -->
      <div class="panel mb-3">
        <div class="sec-head">
          <h4 class="sec-title" style="margin-bottom: 0">块明细（每块的公开分量）</h4>
          <DetailToggle />
        </div>
        <p class="note">
          每一块的<b>分量</b>就是它的指纹：验证时比的就是它（「块哈希层」比的是
          [密文与分量对不对得上]，所以缺一块、换一块都会在这里露出来）。
          鼠标停在任意指纹上会显示<b>完整十六进制</b>。
        </p>
        <el-table :data="file.layout || []" size="small" border max-height="360">
          <el-table-column prop="block_idx" label="块号" width="70" align="center" />
          <el-table-column label="全局下标" width="90" align="center">
            <template #default="{ row }"><span class="mono">{{ row.global_index }}</span></template>
          </el-table-column>
          <el-table-column v-if="detailed" label="分量指纹（十六进制 · 悬浮看完整）" min-width="240">
            <template #default="{ row }">
              <HashText v-if="row.element" :value="row.element" :len="24" />
              <span v-else class="text-3">—</span>
            </template>
          </el-table-column>
          <el-table-column label="明文长度" width="100" align="center">
            <template #default="{ row }"><span class="mono">{{ row.plain_len }} B</span></template>
          </el-table-column>
          <el-table-column label="存在哪几台（主副本在前）" min-width="210">
            <template #default="{ row }">
              <el-tag
                v-for="(r, i) in row.replicas || [row.holder]"
                :key="r"
                :type="i === 0 ? 'success' : 'info'"
                size="small"
                effect="plain"
                class="rep"
              >{{ r }}{{ i === 0 ? ' · 主' : '' }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
        <p v-if="!detailed" class="note mt-2" style="margin-bottom: 0">
          现在是最简略态：只列块号、下标、明文长度与所在节点。点右上角「详细」
          可以看到每块的分量指纹（悬浮看完整十六进制）—— 那一次会多问后端要一份
          分量数据，所以切换时会重新拉一次详情。
        </p>
      </div>

      <div class="panel mb-3">
        <h4 class="sec-title">写操作（只有所有者能改）</h4>
        <el-tabs v-model="activeOp">
          <el-tab-pane label="改一块" name="modify">
            <div class="write-form">
              <div>
                <span class="text-2">第几块：</span>
                <el-input-number v-model="modifyForm.blockIdx" :min="0" :max="Math.max(0, (file.block_count || 1) - 1)" />
              </div>
              <el-input v-model="modifyForm.text" type="textarea" :rows="3" placeholder="新内容（会重新加密、换密钥）" />
              <el-button type="primary" :disabled="!isMine" :loading="writeRunning" @click="doModify">改块</el-button>
            </div>
          </el-tab-pane>
          <el-tab-pane label="追加" name="append">
            <div class="write-form">
              <el-input v-model="appendText" type="textarea" :rows="3" placeholder="要追加到末尾的内容" />
              <el-button type="primary" :disabled="!isMine" :loading="writeRunning" @click="doAppend">追加</el-button>
              <p class="text-3" style="font-size: 12px">
                追加不回头填上一块的空位，所以追加后的切法和重新上传不完全一样。
              </p>
            </div>
          </el-tab-pane>
          <el-tab-pane label="截断" name="truncate">
            <div class="write-form">
              <div>
                <span class="text-2">删除末尾几块：</span>
                <el-input-number v-model="dropBlocks" :min="1" :max="Math.max(1, file.block_count - 1)" />
              </div>
              <el-button type="danger" :disabled="!isMine" :loading="writeRunning" @click="doTruncate">截断</el-button>
              <el-alert type="warning" :closable="false" class="trunc-warn" title="只能删全局向量末尾">
                <template #default>
                  <p style="font-size: 12px">① 只能删全局向量末尾的连续一段，所以实际上只有最后写进向量的那份文件删得动尾巴；别的会报 400，并告诉你是哪个下标卡住了。</p>
                  <p style="font-size: 12px">② 不能删到一块不剩。此操作不可撤销。</p>
                </template>
              </el-alert>
            </div>
          </el-tab-pane>
        </el-tabs>
        <StageTimeline v-if="writeTimings" :timings="writeTimings" class="mt-3" />
      </div>

      <div class="panel">
        <h4 class="sec-title">解密预览</h4>
        <el-button type="primary" :loading="decrypting" @click="doDecrypt">解密</el-button>
        <div v-if="decryptResult" class="mt-3">
          <div class="text-2 mono" style="font-size: 12px">{{ decryptResult.bytes }} 字节</div>
          <div v-if="decodePreview" class="decrypt-text">
            <el-alert
              v-if="decodePreview.head || decodePreview.tail"
              type="warning"
              :closable="false"
              class="mb-2"
              :title="`块边界有半截字符（head=${decodePreview.head}, tail=${decodePreview.tail}）`"
              description="改这一块会连带弄坏相邻那个汉字。"
            />
            <el-alert
              v-if="hasBadBytes(decryptResult.data_hex)"
              type="info"
              :closable="false"
              class="mb-2"
              title="内容里有读不出的字节（可能不是纯文本）"
            />
            <pre class="plain">{{ decodePreview.text }}</pre>
          </div>
        </div>
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
.sec-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}
.rep {
  margin-right: 6px;
}
.meta {
  display: flex;
  flex-wrap: wrap;
  gap: 20px;
}
.meta-item {
  display: flex;
  gap: 8px;
  align-items: baseline;
  font-size: 13px;
}
.meta-item .k {
  color: var(--text-3);
}
.digest {
  margin-top: 10px;
}
.write-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-width: 560px;
}
.trunc-warn {
  max-width: 560px;
}
.plain {
  background: var(--bg-raised);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  padding: 12px;
  font-size: 13px;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-1);
}
.decrypt-text {
  max-width: 720px;
}
</style>
