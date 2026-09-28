<script setup>
/**
 * 视频背景（当前只用在登录页）。
 *
 * 三条底线，少一条在真机上就会翻车：
 *  1. `muted + playsinline + loop` —— 少任何一个，浏览器都会直接拒绝自动播放，
 *     表现是「一片静止的黑」而不是报错；
 *  2. `poster` —— 视频数据到达之前先顶上静图，避免先黑一下；
 *  3. 该关的时候真的关 —— 三种情况之一成立就不加载视频（省流量，也尊重设置）：
 *     系统开了「减少动效」、浏览器开了「节省流量」、或「界面偏好」里关掉了动效。
 *     此时直接退回调用方原有的 CSS 背景。
 *
 * ★ 它是**叠加层**，不是替换层：调用方的 CSS 背景必须一直在，
 *   这样视频没来 / 被跳过 / 加载失败时，页面看起来仍然是完整的。
 */
import { ref, computed, onMounted } from 'vue'

const props = defineProps({
  src: { type: String, default: '/media/login-bg.mp4' },
  poster: { type: String, default: '/media/login-bg-poster.jpg' },
  /** 是否自带一层遮罩。半透明视频叠在深色背景上时，没有遮罩文字会飘。 */
  scrim: { type: Boolean, default: true },
})

const videoEl = ref(null)
const failed = ref(false)
const skipped = ref(false)

const show = computed(() => !skipped.value && !failed.value)

onMounted(() => {
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)')?.matches === true
  const saveData = navigator.connection?.saveData === true
  // 「界面偏好」里的动效开关也管这里 —— 关掉动效就别再放视频了。
  const motionOff = localStorage.getItem('vds_motion') === 'off'
  if (reduce || saveData || motionOff) {
    skipped.value = true
    return
  }
  // muted 之下一般不会被拦，但真被拦了就静默退回 CSS 背景，不弹错误。
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
  /* 半透明 —— 让下层的 CSS 光斑与网格透上来。
     不透明的视频会把这页原有的 CSS 背景整块盖掉，那就不是「加一层」而是「换一张脸」了。 */
  opacity: 0.55;
}
.scrim {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse 75% 65% at 50% 42%, transparent 0%, rgba(5, 8, 15, 0.62) 100%),
    linear-gradient(
      90deg,
      rgba(5, 8, 15, 0.88) 0%,
      rgba(5, 8, 15, 0.3) 46%,
      rgba(5, 8, 15, 0.72) 100%
    );
}
</style>
