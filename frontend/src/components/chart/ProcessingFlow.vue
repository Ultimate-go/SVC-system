<script setup>
/**
 * Processing 数据处理链路：用六个语义化 SVG 节点表达从上传到验证的闭环。
 * 动画分成两层：数据包沿轨道顺序传递，当前阶段卡片周期性扫描，
 * 让动效看起来像真实的数据处理，而不是同时闪烁的装饰。
 */
const STAGES = [
  { key: 'upload', label: '上传', en: 'INGEST', detail: '接收原始文件', tone: 'cyan' },
  { key: 'split', label: '分片', en: 'CHUNK', detail: '切成固定大小的块', tone: 'blue' },
  { key: 'commit', label: '承诺生成', en: 'COMMIT', detail: '计算摘要与证明', tone: 'violet' },
  { key: 'store', label: '分布式存储', en: 'REPLICATE', detail: '写入节点并创建副本', tone: 'green' },
  { key: 'query', label: '查询取证', en: 'PROVE', detail: '按全局下标取证', tone: 'amber' },
  { key: 'verify', label: '完整性验证', en: 'VERIFY', detail: '公开验证数据状态', tone: 'rose' },
]
</script>

<template>
  <div class="processing" aria-label="数据处理链路演示">
    <div class="flow-head">
      <div>
        <span class="flow-kicker mono">LIVE PIPELINE / 06 STAGES</span>
        <p class="flow-caption">从明文进入系统，到证明数据仍然完整</p>
      </div>
      <span class="flow-state"><i />实时处理链路</span>
    </div>

    <div class="pipeline">
      <template v-for="(s, i) in STAGES" :key="s.key">
        <div class="stage" :class="`tone-${s.tone}`" :style="{ '--i': i }">
          <div class="stage-topline"><span class="stage-index mono">0{{ i + 1 }}</span><span class="stage-status">READY</span></div>
          <div class="icon-wrap">
            <svg viewBox="0 0 64 64" aria-hidden="true" class="stage-icon">
              <template v-if="s.key === 'upload'">
                <path d="M32 43V15m0 0L21 26m11-11 11 11" class="icon-stroke" />
                <path d="M17 39v10a4 4 0 0 0 4 4h22a4 4 0 0 0 4-4V39" class="icon-stroke" />
                <path d="M13 53h38" class="icon-stroke faint" />
              </template>
              <template v-else-if="s.key === 'split'">
                <rect x="13" y="16" width="38" height="32" rx="4" class="icon-stroke" />
                <path d="M26 16v32M39 16v32M13 32h38" class="icon-stroke faint" />
                <circle cx="20" cy="24" r="2" class="icon-fill" /><circle cx="33" cy="40" r="2" class="icon-fill" /><circle cx="46" cy="24" r="2" class="icon-fill" />
              </template>
              <template v-else-if="s.key === 'commit'">
                <path d="M32 11 48 17v13c0 11-6.5 18.5-16 23-9.5-4.5-16-12-16-23V17l16-6Z" class="icon-stroke" />
                <path d="m24 31 5 5 11-12" class="icon-stroke" />
              </template>
              <template v-else-if="s.key === 'store'">
                <ellipse cx="32" cy="17" rx="17" ry="7" class="icon-stroke" />
                <path d="M15 17v14c0 4 7.6 7 17 7s17-3 17-7V17M15 31v14c0 4 7.6 7 17 7s17-3 17-7V31" class="icon-stroke" />
                <path d="M32 24v7" class="icon-stroke faint" />
              </template>
              <template v-else-if="s.key === 'query'">
                <circle cx="28" cy="28" r="13" class="icon-stroke" /><path d="m38 38 12 12" class="icon-stroke" />
                <path d="M22 28h12M28 22v12" class="icon-stroke faint" />
              </template>
              <template v-else>
                <path d="M32 11 50 18v13c0 12-7.5 19-18 23-10.5-4-18-11-18-23V18l18-7Z" class="icon-stroke" />
                <path d="m23 32 6 6 13-14" class="icon-stroke" />
              </template>
            </svg>
            <span class="icon-pulse" />
          </div>
          <div class="stage-label">{{ s.label }}</div>
          <div class="stage-en mono">{{ s.en }}</div>
          <div class="stage-detail">{{ s.detail }}</div>
        </div>
        <div v-if="i < STAGES.length - 1" class="link" :style="{ '--i': i }" aria-hidden="true">
          <span class="track" /><span class="packet" /><span class="arrow">›</span>
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.processing { padding: 2px 0 4px; overflow: hidden; }
.flow-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 16px; margin-bottom: 18px; }
.flow-kicker { color: var(--text-3); font-size: 10px; letter-spacing: .14em; }
.flow-caption { color: var(--text-2); font-size: 12px; margin: 5px 0 0; }
.flow-state { display: inline-flex; align-items: center; gap: 7px; color: var(--text-3); font-size: 11px; white-space: nowrap; }
.flow-state i { width: 6px; height: 6px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 0 4px color-mix(in srgb, var(--ok) 13%, transparent); }
.pipeline { display: flex; align-items: stretch; gap: 0; min-width: 690px; }
.stage { --stage-color: var(--accent); position: relative; flex: 1 1 112px; min-width: 106px; padding: 11px 10px 12px; border: 1px solid color-mix(in srgb, var(--stage-color) 30%, var(--line)); border-radius: 8px; background: linear-gradient(145deg, color-mix(in srgb, var(--stage-color) 8%, var(--bg-raised)), var(--bg-panel)); animation: stage-scan 4.8s ease-in-out infinite; animation-delay: calc(var(--i) * .8s); }
.stage::after { content: ''; position: absolute; inset: 0; border-radius: inherit; border-top: 2px solid var(--stage-color); opacity: .7; pointer-events: none; }
.tone-cyan { --stage-color: #22d3ee; }.tone-blue { --stage-color: #60a5fa; }.tone-violet { --stage-color: #a78bfa; }.tone-green { --stage-color: #34d399; }.tone-amber { --stage-color: #fbbf24; }.tone-rose { --stage-color: #fb7185; }
.stage-topline { display: flex; justify-content: space-between; align-items: center; color: var(--text-3); font-size: 9px; letter-spacing: .08em; }
.stage-status { color: var(--stage-color); font-size: 8px; opacity: .8; }
.stage-index { color: var(--stage-color); }
.icon-wrap { position: relative; width: 48px; height: 48px; margin: 8px auto 6px; display: grid; place-items: center; color: var(--stage-color); }
.stage-icon { width: 42px; height: 42px; overflow: visible; position: relative; z-index: 1; }
.icon-stroke { fill: none; stroke: currentColor; stroke-width: 2.5; stroke-linecap: round; stroke-linejoin: round; }
.icon-stroke.faint { opacity: .55; stroke-width: 1.8; }.icon-fill { fill: currentColor; }
.icon-pulse { position: absolute; inset: 6px; border: 1px solid currentColor; border-radius: 50%; opacity: .12; animation: icon-ring 2.4s ease-out infinite; animation-delay: calc(var(--i) * .8s); }
.stage-label { color: var(--text-1); font-size: 13px; font-weight: 600; text-align: center; white-space: nowrap; }
.stage-en { color: var(--stage-color); font-size: 9px; letter-spacing: .12em; text-align: center; margin-top: 3px; }
.stage-detail { color: var(--text-3); font-size: 10px; line-height: 1.4; text-align: center; margin-top: 8px; min-height: 28px; }
.link { position: relative; flex: 0 0 30px; height: 2px; align-self: center; margin: -28px 2px 0; }
.track { position: absolute; inset: 0; background: var(--line-strong); }
.track::after { content: ''; display: block; width: 100%; height: 100%; background: var(--accent); transform: scaleX(0); transform-origin: left; animation: track-flow 4.8s ease-in-out infinite; animation-delay: calc(var(--i) * .8s + .15s); }
.packet { position: absolute; top: 50%; left: 0; width: 6px; height: 6px; border-radius: 50%; background: var(--accent); box-shadow: 0 0 8px var(--accent); transform: translate(-50%, -50%); animation: packet-move 4.8s linear infinite; animation-delay: calc(var(--i) * .8s + .1s); }
.arrow { position: absolute; right: -2px; top: 50%; transform: translateY(-57%); color: var(--text-3); font-size: 15px; }
@keyframes stage-scan { 0%, 14%, 100% { transform: translateY(0); box-shadow: none; } 5%, 10% { transform: translateY(-3px); box-shadow: 0 8px 24px color-mix(in srgb, var(--stage-color) 14%, transparent); } }
@keyframes icon-ring { 0%, 65%, 100% { transform: scale(.7); opacity: 0; } 15% { transform: scale(1.25); opacity: .35; } }
@keyframes track-flow { 0%, 12% { transform: scaleX(0); } 42%, 100% { transform: scaleX(1); } }
@keyframes packet-move { 0%, 12% { left: 0; opacity: 0; } 20% { opacity: 1; } 78% { opacity: 1; } 90%, 100% { left: 100%; opacity: 0; } }
@media (max-width: 900px) { .processing { overflow-x: auto; padding-bottom: 10px; } .flow-head { align-items: flex-start; } }
@media (max-width: 560px) { .flow-state { display: none; } }
@media (prefers-reduced-motion: reduce) { .stage, .icon-pulse, .track::after, .packet { animation: none; } }
</style>
