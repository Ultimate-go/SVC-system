<script setup>
/**
 * 视频背景。当前仅登录页使用。
 *
 * 播放前提（任一不满足都会导致自动播放被拒）：
 *  1. `muted + playsinline + loop` 三者必须同时具备；
 *  2. `poster` 在视频数据到达前显示静态图，避免先出现黑屏；
 *  3. 下列任一条件成立时不得加载视频，直接使用调用方的 CSS 背景：
 *     系统启用「减少动效」、浏览器启用「节省流量」、界面偏好中关闭动效。
 *
 * 本组件是叠加层：调用方的 CSS 背景必须始终存在，
 * 使视频未到达、被跳过或加载失败时页面保持完整。
 */
import { ref, computed, onMounted } from 'vue'

const props = defineProps({
  src: { type: String, default: '/media/login-bg.mp4' },
  poster: { type: String, default: '/media/login-bg-poster.jpg' },
  /** 是否自带遮罩。半透明视频叠在深色背景上时，无遮罩会使文字对比不足。 */
  scrim: { type: Boolean, default: true },
})

const videoEl = ref(null)
const failed = ref(false)
const skipped = ref(false)

const show = computed(() => !skipped.value && !failed.value)

onMounted(() => {
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true
  const saveData = navigator.connection?.saveData === true
  // 界面偏好中的动效开关同样作用于本组件：关闭动效时不加载视频。
  const motionOff = localStorage.getItem('vds_motion') === 'off'
  if (reduce || saveData || motionOff) {
    skipped.value = true
    return
  }
  // 静音状态下通常不会被拦截；若被拦截则静默退回 CSS 背景，不提示错误。
  videoEl.value?.play?.().catch(() => (failed.value = true))
})
</script>

<template>
  <div class="video-backdrop" aria-hidden="true">
    <video
      v-if="show"
      ref="videoEl"
      class="video"
      :src="src"
      :poster="poster"
      autoplay
      muted
      loop
      playsinline
      preload="auto"
      tabindex="-1"
      @error="failed = true"
    />
    <div v-if="show && scrim" class="scrim" />
  </div>
</template>

<style scoped>
.video-backdrop {
  position: absolute;
  inset: 0;
  overflow: hidden;
  pointer-events: none;
}
.video {
  width: 100%;
  height: 100%;
  object-fit: cover;
  /* 半透明：使下层的 CSS 光斑与网格可见。
     不透明会完全覆盖页面原有背景。
     本组透明度与下方 scrim 两端配套：素材为 public/media/login-bg.mp4（15 s、3 Mbps）。 */
  opacity: 0.8;
}
.scrim {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse 75% 65% at 50% 42%, transparent 0%, rgba(5, 8, 15, 0.62) 100%),
    linear-gradient(
      90deg,
      rgba(5, 8, 15, 0.5) 0%,
      rgba(5, 8, 15, 0.3) 46%,
      rgba(5, 8, 15, 0.5) 100%
    );
}
</style>
