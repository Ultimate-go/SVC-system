<script setup>
/**
 * 新增节点向导 —— 这页不是表单页。
 *
 * 后端没有「新增节点」接口 —— 节点是 python scripts/run_nodes.py 起的独立进程，
 * 地址与令牌写在环境变量里，改台数还要求 --reset 重建库。
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
    <PageHeader title="新增存储节点" subtitle="这一页不保存设置，只生成命令" />

    <el-alert type="info" :closable="false" class="mb-3" title="后端没有「新增节点」接口：节点是独立进程，地址与令牌写在环境变量里；改台数需要 --reset 重建库。" />

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
      <h4 class="sec-title mt-3">2 · 环境变量（与上面的 id 一一对应）</h4>
      <pre class="cmd mono">{{ generated.urls }}
{{ generated.ids }}
{{ generated.tokenLine }}</pre>
    </div>

    <el-alert type="warning" :closable="false" class="mt-3" title="三条前提">
      <template #default>
        <ol class="premises">
          <li>VDS_NODE_IDS 与 VDS_NODE_URLS 必须列出同样的节点，否则启动闸拒绝启动。</li>
          <li>改台数必须 --reset：单进程模式与跨进程模式的库不能混用。</li>
          <li>漏设环境变量会让后端静默退回单进程模式，然后被启动闸以「节点与协调者不同步」拒绝启动。</li>
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
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 10px;
}
.cmd {
  background: var(--bg-raised);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  padding: 12px;
  font-size: 12px;
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
