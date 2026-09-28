<script setup>
/**
 * 总览页。普通用户访问 / 时重定向到 /files。
 * 管理员看到：公开参数、全局摘要 δ、规模、集群健康、密钥模型、待补推横幅、自检。
 */
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../../../stores/auth'
import { systemApi } from '../../../api/system'
import { devicesApi } from '../../../api/devices'
import { hexFp } from '../../../utils/format'
import StatCard from '../../../components/common/StatCard.vue'
import RingGauge from '../../../components/chart/RingGauge.vue'
import ProcessingFlow from '../../../components/chart/ProcessingFlow.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'
import Icon from '../../../components/icons/Icon.vue'

const auth = useAuthStore()
const router = useRouter()

const loading = ref(true)
const error = ref('')
const status = ref(null)
const pending = ref(null)
const checking = ref(false)
const checkResult = ref(null)

const isAdmin = computed(() => auth.user?.role === 'admin')

const delta = computed(() => status.value?.delta || {})
const crs = computed(() => status.value?.crs || {})
const keywrap = computed(() => status.value?.keywrap || {})

const hasPending = computed(() => {
  const p = pending.value
  return !!(p?.pending || p?.persist_pending)
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [s, p] = await Promise.all([systemApi.status(), devicesApi.pending()])
    status.value = s.data
    pending.value = p.data
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function runCheck() {
  checking.value = true
  checkResult.value = null
  try {
    const { data } = await systemApi.check()
    checkResult.value = data
    ElMessage.success('自检通过')
  } catch (e) {
    checkResult.value = { ok: false, message: e?.response?.data?.detail || '自检失败' }
  } finally {
    checking.value = false
  }
}

onMounted(() => {
  if (!isAdmin.value) {
    router.replace('/files')
    return
  }
  load()
})
</script>

<template>
  <div v-if="loading" class="loading" v-loading="true" />
  <div v-else-if="error" class="panel">
    <p class="text-danger">{{ error }}</p>
    <el-button class="mt-3" @click="load">重试</el-button>
  </div>
  <div v-else-if="status" class="dashboard">
    <el-alert
      v-if="hasPending"
      type="warning"
      :closable="false"
      class="mb-3"
      title="有写推未完成 —— 全网摘要暂时不一致"
      description="不要重新上传（登记表已分配过那批下标，重传会拿到新下标）。请到「设备」页点「补推」。"
    >
      <template #default>
        <el-button size="small" @click="router.push('/devices')">去补推</el-button>
      </template>
    </el-alert>

    <div class="panel mb-3">
      <h3 class="sec-title">Processing · 数据处理链路</h3>
      <ProcessingFlow />
    </div>

    <div class="grid">
      <StatCard label="公开参数" :value="crs.N_bits" unit="位" hint="隐藏阶群的模数位长" />
      <StatCard label="块哈希" :value="crs.l" unit="位" hint="每段密文的比特数" />
      <StatCard label="素数位长" :value="crs.prime_bits" unit="位" hint="块哈希+1" />
      <StatCard label="位置上限" :value="crs.n_max" hint="不可更改" />
    </div>

    <div class="panel mt-4">
      <h3 class="sec-title">全局摘要 δ = (U, C, n)</h3>
      <div class="delta-row">
        <RingGauge :value="delta.n" :max="crs.n_max" label="已用位置" />
        <div class="delta-detail">
          <div class="delta-item">
            <span class="k">n</span>
            <span class="v mono">{{ delta.n }}</span>
            <span class="hint">全局块数</span>
          </div>
          <div class="delta-item">
            <span class="k">U</span>
            <span class="v mono" :title="delta.U">{{ hexFp(delta.U, 16) }}</span>
            <span class="hint">十六进制指纹（前 16 位）</span>
          </div>
          <div class="delta-item">
            <span class="k">C</span>
            <span class="v mono" :title="delta.C">{{ hexFp(delta.C, 16) }}</span>
            <span class="hint">十六进制指纹（前 16 位）</span>
          </div>
        </div>
      </div>
    </div>

    <div class="grid mt-4">
      <StatCard label="文件数" :value="status.files" hint="全系统所有文件" />
      <StatCard label="块数" :value="status.blocks" hint="全局向量已用位置" />
      <StatCard label="副本数" :value="status.replica_factor" hint="每块默认存几份" />
      <StatCard
        label="副本不足"
        :value="status.under_replicated"
        :hint="status.under_replicated ? '开副本之前传的老数据，重传可补齐' : '无'"
      />
    </div>

    <div class="panel mt-4">
      <h3 class="sec-title">集群健康</h3>
      <div class="cluster-row">
        <span class="cluster-item">模式 <b class="mono">{{ status.mode }}</b></span>
        <span class="cluster-item">传输 <b class="mono">{{ status.transport }}</b></span>
        <span class="cluster-item">
          节点 <b class="mono">{{ status.nodes }}</b> 在线
        </span>
        <span v-if="status.nodes_down?.length" class="cluster-item text-danger">
          离线：<b class="mono">{{ status.nodes_down.join(', ') }}</b>
        </span>
        <span v-else class="cluster-item text-ok">全部在线</span>
      </div>
    </div>

    <div class="panel mt-4">
      <h3 class="sec-title">密钥模型</h3>
      <div class="keywrap">
        <div class="keywrap-item"><span class="k">方案</span><span class="v">{{ keywrap.scheme }}</span></div>
        <div class="keywrap-item"><span class="k">用户 / 有密钥</span><span class="v mono">{{ keywrap.users }} / {{ keywrap.user_keys }}</span></div>
        <div class="keywrap-item"><span class="k">会话私钥在内存</span><span class="v mono">{{ keywrap.sessions_with_key }}</span></div>
        <div class="keywrap-item"><span class="k">私钥存储位置</span><span class="v">{{ keywrap.private_key_storage }}</span></div>
      </div>
    </div>

    <div class="panel mt-4">
      <h3 class="sec-title">自检</h3>
      <el-button :loading="checking" @click="runCheck">
        <Icon name="refresh" :size="14" style="margin-right: 6px" />启动自检
      </el-button>
      <div v-if="checkResult" class="mt-3">
        <el-alert
          :type="checkResult.ok ? 'success' : 'error'"
          :closable="false"
          :title="checkResult.message"
        />
        <StageTimeline v-if="checkResult.timings" :timings="checkResult.timings" class="mt-3" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.loading {
  min-height: 200px;
}
.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}
.sec-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 14px;
}
.delta-row {
  display: flex;
  gap: 28px;
  align-items: center;
}
.delta-detail {
  display: flex;
  flex-direction: column;
  gap: 10px;
  flex: 1;
}
.delta-item {
  display: flex;
  align-items: baseline;
  gap: 12px;
}
.delta-item .k {
  font-family: var(--font-mono);
  color: var(--accent);
  font-weight: 500;
  width: 20px;
}
.delta-item .v {
  color: var(--text-1);
  font-size: 15px;
}
.delta-item .hint {
  color: var(--text-3);
  font-size: 12px;
}
.cluster-row {
  display: flex;
  flex-wrap: wrap;
  gap: 24px;
  font-size: 13px;
  color: var(--text-2);
}
.keywrap-item {
  display: flex;
  gap: 12px;
  padding: 6px 0;
  font-size: 13px;
}
.keywrap-item .k {
  color: var(--text-3);
  width: 140px;
  flex-shrink: 0;
}
.keywrap-item .v {
  color: var(--text-1);
}
.keywrap-note {
  font-size: 13px;
  color: var(--text-2);
  margin-top: 8px;
}
</style>
