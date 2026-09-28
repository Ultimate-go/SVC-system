<script setup>
/**
 * Processing 数据链路图 —— 演示数据处理全链路。
 *
 * 上传 → 分片 → 承诺生成 → 存储 → 查询 → 验证，六个节点依次流动：
 *   - 数据包沿连线移动（CSS 关键帧）
 *   - 连线按顺序「点亮」流动
 *   - 节点依次高亮 + 进度脉冲
 *
 * 纯 CSS 动画，零依赖，性能友好（只用 transform / opacity，
 * 且包在 prefers-reduced-motion 里）。
 */

const STAGES = [
  { key: 'upload', label: '上传', en: 'UPLOAD' },
  { key: 'split', label: '分片', en: 'SPLIT' },
  { key: 'commit', label: '承诺生成', en: 'COMMIT' },
  { key: 'store', label: '存储', en: 'STORE' },
  { key: 'query', label: '查询', en: 'QUERY' },
  { key: 'verify', label: '验证', en: 'VERIFY' },
]
</script>

<template>
  <div class="processing" aria-label="数据处理链路演示">
    <div class="pipeline">
      <template v-for="(s, i) in STAGES" :key="s.key">
        <div class="node" :style="{ '--i': i }">
          <div class="node-dot" />
          <div class="node-label">
            <span class="node-name">{{ s.label }}</span>
            <span class="node-en mono">{{ s.en }}</span>
          </div>
        </div>
        <div v-if="i < STAGES.length - 1" class="link" :style="{ '--i': i }">
          <span class="packet" />
        </div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.processing {
  padding: 6px 0;
  overflow: hidden;
}
.pipeline {
  display: flex;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 0;
}
.node {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: 96px;
  position: relative;
  animation: node-pulse 3.6s ease-in-out infinite;
  animation-delay: calc(var(--i) * 0.6s);
}
.node-dot {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--bg-raised);
  border: 2px solid var(--accent);
  position: relative;
  z-index: 1;
  transition: background 0.3s ease, box-shadow 0.3s ease;
}
.node:hover .node-dot {
  background: var(--accent);
  box-shadow: 0 0 12px rgba(34, 211, 238, 0.6);
}
.node-label {
  margin-top: 8px;
  text-align: center;
}
.node-name {
  display: block;
  font-size: 13px;
  color: var(--text-1);
}
.node-en {
  display: block;
  font-size: 10px;
  color: var(--text-3);
  letter-spacing: 0.08em;
  margin-top: 2px;
}

/* 连线：与节点水平对齐（节点宽 96px，连线占 48px，居中于两节点之间） */
.link {
  flex: 1;
  min-width: 40px;
  max-width: 72px;
  height: 2px;
  margin-top: 8px;
  background: var(--line-strong);
  position: relative;
  overflow: hidden;
}
.link::after {
  content: '';
  position: absolute;
  inset: 0;
  background: var(--accent);
  transform: translateX(-100%);
  animation: link-flow 3.6s ease-in-out infinite;
  animation-delay: calc(var(--i) * 0.6s + 0.15s);
}

/* 数据包：一个沿连线移动的小光点 */
.packet {
  position: absolute;
  top: 50%;
  left: 0;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent-2);
  transform: translateY(-50%) translateX(0);
  animation: packet-move 3.6s linear infinite;
  animation-delay: calc(var(--i) * 0.6s);
  opacity: 0;
}

@keyframes node-pulse {
  0%, 100% {
    transform: scale(1);
  }
  50% {
    transform: scale(1.06);
  }
}
@keyframes link-flow {
  0% {
    transform: translateX(-100%);
  }
  60%, 100% {
    transform: translateX(100%);
  }
}
@keyframes packet-move {
  0% {
    opacity: 0;
    transform: translateY(-50%) translateX(0);
  }
  15% {
    opacity: 1;
  }
  85% {
    opacity: 1;
  }
  100% {
    opacity: 0;
    transform: translateY(-50%) translateX(100%);
  }
}

@media (prefers-reduced-motion: reduce) {
  .node,
  .link::after,
  .packet {
    animation: none;
  }
}
</style>
