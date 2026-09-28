<script setup>
/**
 * 性能页。
 *
 * 数据源只有 src/data/measured.js（实测记录值），不编数。
 * 每块标来源。唯一允许「此刻现取」的是当前规模（/api/status 的 blocks 等）。
 */
import { ref, onMounted } from 'vue'
import { systemApi } from '../../../api/system'
import {
  CFG_L256,
  CFG_L128,
  QUERY_SWEEP,
  BATCH_TRIAL,
  ONE_TIME,
  UPLOAD_COST,
  SCALE_LAWS,
  L_COMPARE,
  MISC,
} from '../../../data/measured'
import PageHeader from '../../../components/common/PageHeader.vue'
import BarMeter from '../../../components/chart/BarMeter.vue'

const live = ref(null)

const maxQueryProve = Math.max(...QUERY_SWEEP.map((r) => r.prove + r.aggregate + r.verify))
const maxBatch = Math.max(...BATCH_TRIAL.rows.map((r) => r.ms))
const maxOneTime = Math.max(...ONE_TIME.map((r) => r.ms))

onMounted(async () => {
  try {
    const { data } = await systemApi.status()
    live.value = data
  } catch {
    live.value = null
  }
})
</script>

<template>
  <div>
    <PageHeader title="性能" subtitle="把量过的数画出来（纯 CSS，不引图表库）" />

    <el-alert type="info" :closable="false" class="mb-3" title="以下均为记录值，不是此刻的实况；把别人量过的数当成实况是谎报。" />

    <div class="panel mb-3">
      <h4 class="sec-title">配置口径</h4>
      <div class="cfg">
        <div v-for="c in CFG_L256.config" :key="c" class="cfg-item mono">{{ c }}</div>
      </div>
      <p class="src">来源：{{ CFG_L256.source }}</p>
    </div>

    <div class="panel mb-3">
      <h4 class="sec-title">1 · 查询块数 vs 各阶段耗时（证据字节恒为 256）</h4>
      <div class="query-table">
        <div class="qhead mono">
          <span>|Q|</span><span>证明生成</span><span>聚合</span><span>验证</span><span>证据字节</span>
        </div>
        <div v-for="r in QUERY_SWEEP" :key="r.q" class="qrow">
          <span class="mono">{{ r.q }}</span>
          <BarMeter :value="r.prove" :max="maxQueryProve" tone="accent" :suffix="` ms`" class="qbar" />
          <BarMeter :value="r.aggregate" :max="maxQueryProve" tone="accent2" :suffix="` ms`" class="qbar" />
          <BarMeter :value="r.verify" :max="maxQueryProve" tone="ok" :suffix="` ms`" class="qbar" />
          <span class="mono evidence">{{ r.evidence }} B</span>
        </div>
      </div>
      <p class="note">
        证据规模上界 = 2 个群元素 = 256 字节，与查询块数、向量长度都无关 —— 这是 succinct 的硬指标。
        |Q|=14 那行 129 字节是因为此刻 I 覆盖全部位置，空乘积使 Λ_I = 1。
      </p>
    </div>

    <div class="panel mb-3">
      <h4 class="sec-title">2 · 批量验证 vs 逐份验证（照实显示批量更慢）</h4>
      <div class="batch-list">
        <div v-for="r in BATCH_TRIAL.rows" :key="r.what" class="batch-row" :class="{ bad: r.bad }">
          <BarMeter :value="r.ms" :max="maxBatch" :tone="r.bad ? 'danger' : 'accent'" :suffix="` ms`" class="grow" />
          <div class="batch-what">{{ r.what }}</div>
          <div class="batch-note">{{ r.note }}</div>
        </div>
      </div>
      <p class="note">{{ BATCH_TRIAL.why }}</p>
      <p class="note text-accent">{{ BATCH_TRIAL.gained }}</p>
    </div>

    <div class="panel mb-3">
      <h4 class="sec-title">3 · 一次性成本（l = 128 档）</h4>
      <div class="one-time">
        <div v-for="r in ONE_TIME" :key="r.what" class="ot-row">
          <BarMeter :value="r.ms" :max="maxOneTime" tone="warn" :suffix="` ms`" class="grow" />
          <div class="ot-what">{{ r.what }}</div>
        </div>
      </div>
      <p class="note">PrimeGen.first(1024) 是最贵的一次性开销（1442 ms），所以素数表做了全局缓存。</p>
    </div>

    <div class="panel mb-3">
      <h4 class="sec-title">4 · 上传是一次全网事件</h4>
      <div v-for="r in UPLOAD_COST.rows" :key="r.what" class="upload-row">
        <span>{{ r.what }}</span>
        <span class="mono">{{ r.ms }} ms</span>
      </div>
      <p class="note">{{ UPLOAD_COST.note }}</p>
      <p class="note text-accent">{{ UPLOAD_COST.exception }}</p>
    </div>

    <div class="panel mb-3">
      <h4 class="sec-title">5 · 两条参数律</h4>
      <div v-for="r in SCALE_LAWS" :key="r.what" class="law">
        <div class="law-what">{{ r.what }}</div>
        <div class="law-raw mono">{{ r.raw }}</div>
        <div class="law-verdict">{{ r.verdict }}</div>
      </div>
    </div>

    <div class="panel mb-3">
      <h4 class="sec-title">6 · l = 128 与 l = 256 对照</h4>
      <el-table :data="L_COMPARE" border size="small">
        <el-table-column prop="what" label="项" width="180" />
        <el-table-column prop="l128" label="l=128" />
        <el-table-column prop="l256" label="l=256（本档）" />
      </el-table>
    </div>

    <div class="panel mb-3">
      <h4 class="sec-title">7 · 其它</h4>
      <div v-for="r in MISC" :key="r.what" class="misc-row">
        <span>{{ r.what }}</span>
        <span class="mono">{{ r.value }}</span>
      </div>
    </div>

    <div class="panel">
      <h4 class="sec-title">此刻的规模（现取，仅这一处）</h4>
      <div v-if="live" class="mono">
        块数 {{ live.blocks }} · 文件 {{ live.files }} · n_max {{ live.crs.n_max }} · 副本 {{ live.replica_factor }}
      </div>
      <p v-else class="text-3">（获取失败，不影响上方记录值）</p>
    </div>
  </div>
</template>

<style scoped>
.sec-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 14px;
}
.src,
.note {
  font-size: 12px;
  color: var(--text-3);
  margin-top: 8px;
}
.cfg {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 20px;
}
.cfg-item {
  font-size: 13px;
  color: var(--text-1);
}
.query-table {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.qhead,
.qrow {
  display: grid;
  grid-template-columns: 48px 1fr 1fr 1fr 96px;
  gap: 16px;
  align-items: center;
}
.qhead {
  font-size: 12px;
  color: var(--text-3);
}
.qbar {
  width: 100%;
}
.evidence {
  text-align: right;
  color: var(--ok);
}
.batch-list,
.one-time {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.batch-row,
.ot-row {
  display: flex;
  align-items: center;
  gap: 14px;
}
.batch-what,
.ot-what {
  width: 260px;
  flex-shrink: 0;
  font-size: 13px;
  color: var(--text-1);
}
.batch-note {
  font-size: 12px;
  color: var(--text-3);
}
.batch-row.bad .batch-what {
  color: var(--danger);
}
.upload-row,
.misc-row,
.law {
  display: flex;
  gap: 16px;
  padding: 6px 0;
  font-size: 13px;
}
.law {
  flex-direction: column;
  gap: 4px;
  border-bottom: 1px solid var(--line);
}
.law-what {
  color: var(--text-1);
}
.law-raw {
  color: var(--accent);
}
.law-verdict {
  color: var(--text-2);
  font-size: 12px;
}
</style>
