<script setup>
/**
 * 新增节点向导 —— 这页不是表单页。
 *
 * 后端没有「新增节点」接口 —— 节点是 python scripts/run_nodes.py 起的独立进程，
 * 地址与令牌写在环境变量里；改台数在界面上改，重启后生效，数据一行都不动。
 * 所以这一页的价值是「把正确的命令生成出来」，不要放一个假的保存按钮。
 */
import { ref, computed, reactive } from 'vue'
import PageHeader from '../../../components/common/PageHeader.vue'
import Icon from '../../../components/icons/Icon.vue'

const form = reactive({ nodeId: 'node-5', address: 'http://127.0.0.1:9105', token: '' })

const generated = computed(() => {
  const id = form.nodeId.trim()
  const addr = form.address.trim()
  const token = form.token.trim()
  if (!id || !addr || !token) return null
  return {
    runNodes: `python scripts/run_nodes.py --nodes ${id} --base-port ${portFrom(addr)} --token ${token}`,
    urls: `VDS_NODE_URLS = "${id}=${addr}"`,
    ids: `VDS_NODE_IDS = "${id}"`,
    tokenLine: `VDS_NODE_TOKEN = "${token}"`,
  }
})

function portFrom(addr) {
  const m = addr.match(/:(\d+)$/)
  return m ? m[1] : '9105'
}

const basePort = computed(() => portFrom(form.address))

async function copy(text) {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    // 剪贴板不可用时静默
  }
}
</script>

<template>
  <div>
    <PageHeader title="新增存储节点" subtitle="本页不写入任何配置，仅生成启动命令与环境变量" />

    <el-alert type="info" :closable="false" class="mb-3" title="系统未提供「新增节点」接口：存储节点以独立进程运行，其地址与访问令牌由环境变量配置；集群台数在「存储节点」页修改，重启后生效，数据不受影响。" />

    <div class="panel form-panel">
      <el-form :model="form" label-width="110px">
        <el-form-item label="节点 id">
          <el-input v-model="form.nodeId" placeholder="如 node-5" />
        </el-form-item>
        <el-form-item label="地址">
          <el-input v-model="form.address" placeholder="如 http://127.0.0.1:9105" />
        </el-form-item>
        <el-form-item label="节点令牌">
          <el-input v-model="form.token" placeholder="与后端同一个值" show-password />
        </el-form-item>
      </el-form>
    </div>

    <div v-if="generated" class="panel mt-3">
      <h4 class="sec-title">1 · 启动命令</h4>
      <pre class="cmd mono">{{ generated.runNodes }}</pre>
      <h4 class="sec-title mt-3">2 · 环境变量（须与上面的节点 id 一一对应）</h4>
      <pre class="cmd mono">{{ generated.urls }}
{{ generated.ids }}
{{ generated.tokenLine }}</pre>
    </div>

    <el-alert type="warning" :closable="false" class="mt-3" title="三条前提">
      <template #default>
        <ol class="premises">
          <li>VDS_NODE_IDS 与 VDS_NODE_URLS 必须列出相同的节点集合，否则启动闸将拒绝启动。</li>
          <li>集群台数在「存储节点」页修改，重启后生效，数据不受影响；单进程模式与跨进程模式的数据文件不可混用。</li>
          <li>未配置环境变量时，后端会静默回退到单进程模式，随后被启动闸以「节点视图与协调者不同步」为由拒绝启动。</li>
        </ol>
      </template>
    </el-alert>
  </div>
</template>

<style scoped>
.form-panel {
  max-width: 560px;
}
.sec-title {
  font-size: 17px;
  font-weight: 600;
  color: var(--text-1);
  margin-bottom: 10px;
}
.cmd {
  background: var(--bg-raised);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  padding: 12px;
  font-size: 13px;
  overflow-x: auto;
  color: var(--text-1);
  white-space: pre-wrap;
  word-break: break-all;
}
.premises {
  margin: 0;
  padding-left: 20px;
  font-size: 13px;
  line-height: 1.8;
}
</style>
