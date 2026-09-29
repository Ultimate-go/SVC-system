<script setup>
import { ref, reactive } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../../stores/auth'
import Icon from '../../components/icons/Icon.vue'
import VideoBackdrop from '../../components/media/VideoBackdrop.vue'

const auth = useAuthStore()
const router = useRouter()
const route = useRoute()

const form = reactive({ username: '', password: '' })
const loading = ref(false)

async function submit() {
  if (!form.username || !form.password) {
    ElMessage.warning('请输入账号与密码')
    return
  }
  loading.value = true
  try {
    await auth.login(form.username, form.password)
    const redirect = route.query.redirect || '/'
    router.replace(String(redirect))
  } catch {
    // 错误已由 axios 拦截器原样弹出（401/403/409 的中文理由）。
  } finally {
    loading.value = false
  }
}

/** 背景漂浮的英文单词。 */
const FLOATING_WORDS = ['SECURE', 'VERIFIABLE', 'INTEGRITY', 'COMMITMENT', 'CRYPTOGRAPHIC', 'DECENTRALIZED']
</script>

<template>
  <div class="login-page">
    <!-- 深色科技感背景：CSS 底座 + 半透明视频叠层 -->
    <div class="backdrop" aria-hidden="true">
      <div class="bg-glow" />
      <div class="bg-grid" />
      <VideoBackdrop />
      <span
        v-for="(w, i) in FLOATING_WORDS"
        :key="w"
        class="bg-word mono"
        :style="{ '--i': i, '--left': (8 + i * 16) + '%', '--delay': (i * 1.1) + 's' }"
      >{{ w }}</span>
    </div>

    <div class="login-split">
      <!-- 左：项目介绍 -->
      <section class="intro">
        <div class="intro-inner">
          <div class="intro-tag mono">VERIFIABLE · DECENTRALIZED · CRYPTOGRAPHIC</div>
          <h1 class="intro-title">基于增量聚合向量承诺的<br />可验证分布式存储与查询系统</h1>
          <p class="intro-sub">
            文件交给别人保管，凭什么放心？
            <br />答案不是「请相信我们」，而是一份谁都能自己验证的密码学证据。
          </p>
          <ul class="intro-points">
            <li><span class="pt-dot" />谁都能验证，只有所有者能解密</li>
            <li><span class="pt-dot" />不靠承诺，靠可验证</li>
            <li><span class="pt-dot" />384 个算法回归测试钉住密码学</li>
          </ul>
        </div>
      </section>

      <!-- 右：登录面板 -->
      <section class="login-side">
        <div class="login-card">
          <h2 class="card-title">登录</h2>
          <p class="card-sub">请输入账号与密码</p>

          <!-- 被 401 兜底送回来时才会出现（见 main.js 的 expired 标记）。
               用常驻提示而不是弹窗：掉线是“后端重启”的必然结果，不是错误。 -->
          <el-alert
            v-if="route.query.expired === '1'"
            type="info"
            :closable="false"
            class="mb-3"
            title="登录已过期，请重新登录"
            description="后端重启会换掉签名密钥，重登一次就好。"
          />

          <div class="form">
            <el-input
              v-model="form.username"
              placeholder="账号"
              size="large"
              @keyup.enter="submit"
            >
              <template #prefix><Icon name="user" :size="15" /></template>
            </el-input>
            <el-input
              v-model="form.password"
              type="password"
              placeholder="密码"
              size="large"
              show-password
              @keyup.enter="submit"
            >
              <template #prefix><Icon name="lock" :size="15" /></template>
            </el-input>
            <el-button
              type="primary"
              size="large"
              :loading="loading"
              class="submit"
              @click="submit"
            >
              登录
            </el-button>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  position: relative;
  min-height: 100vh;
  display: flex;
  align-items: stretch;
  background: #05080f;
  overflow: hidden;
}

/* ---------- 深色科技感动态背景 ---------- */
.backdrop {
  position: absolute;
  inset: 0;
  pointer-events: none;
  overflow: hidden;
}
.bg-glow {
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse 60% 50% at 20% 30%, rgba(34, 211, 238, 0.16), transparent 60%),
    radial-gradient(ellipse 50% 50% at 80% 70%, rgba(99, 102, 241, 0.14), transparent 60%),
    radial-gradient(ellipse 40% 40% at 60% 20%, rgba(34, 197, 94, 0.08), transparent 60%);
  animation: glow-drift 12s ease-in-out infinite alternate;
}
.bg-grid {
  position: absolute;
  inset: 0;
  background:
    repeating-linear-gradient(0deg, rgba(120, 190, 255, 0.06) 0, rgba(120, 190, 255, 0.06) 1px, transparent 1px, transparent 48px),
    repeating-linear-gradient(90deg, rgba(120, 190, 255, 0.06) 0, rgba(120, 190, 255, 0.06) 1px, transparent 1px, transparent 48px);
  mask-image: radial-gradient(ellipse 80% 70% at 50% 50%, #000 40%, transparent 100%);
}

/* 漂浮的英文单词 */
.bg-word {
  position: absolute;
  top: 0;
  left: var(--left);
  font-size: 13px;
  letter-spacing: 0.3em;
  color: rgba(120, 190, 255, 0.14);
  white-space: nowrap;
  animation: word-rise 18s linear infinite;
  animation-delay: var(--delay);
}
@keyframes word-rise {
  0% {
    transform: translateY(105vh);
    opacity: 0;
  }
  10% {
    opacity: 1;
  }
  90% {
    opacity: 1;
  }
  100% {
    transform: translateY(-10vh);
    opacity: 0;
  }
}
@keyframes glow-drift {
  0% {
    transform: translate(0, 0) scale(1);
  }
  100% {
    transform: translate(2%, -3%) scale(1.06);
  }
}

/* ---------- 左右分栏 ---------- */
.login-split {
  position: relative;
  z-index: 1;
  display: flex;
  width: 100%;
  max-width: 1200px;
  margin: 0 auto;
  padding: 40px 32px;
  gap: 40px;
  align-items: center;
}

/* 左：项目介绍 */
.intro {
  flex: 1.2;
  color: #e6edf7;
}
.intro-inner {
  max-width: 560px;
}
.intro-tag {
  font-size: 11px;
  letter-spacing: 0.22em;
  color: var(--accent);
  margin-bottom: 20px;
}
.intro-title {
  font-size: 34px;
  font-weight: 500;
  line-height: 1.35;
  margin: 0 0 20px;
  background: linear-gradient(120deg, #e6edf7, #7dd3fc);
  -webkit-background-clip: text;
  background-clip: text;
  -webkit-text-fill-color: transparent;
}
.intro-sub {
  font-size: 14px;
  line-height: 1.8;
  color: #93a4bf;
  margin: 0 0 24px;
}
.intro-points {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.intro-points li {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 14px;
  color: #cbd5e1;
}
.pt-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--accent);
  box-shadow: 0 0 8px rgba(34, 211, 238, 0.6);
  flex-shrink: 0;
}

/* 右：登录面板 */
.login-side {
  flex: 1;
  display: flex;
  justify-content: flex-end;
}
.login-card {
  width: 380px;
  max-width: 100%;
  padding: 36px 32px;
  background: rgba(13, 20, 36, 0.75);
  border: 1px solid rgba(120, 190, 255, 0.16);
  border-radius: 14px;
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.05), 0 20px 60px rgba(0, 0, 0, 0.4);
  backdrop-filter: blur(12px);
}
.card-title {
  font-size: 22px;
  font-weight: 500;
  color: #e6edf7;
  margin: 0 0 6px;
}
.card-sub {
  font-size: 13px;
  color: #93a4bf;
  margin: 0 0 28px;
}
.form {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.submit {
  width: 100%;
  margin-top: 4px;
}

/* 移动端：左右分栏自动堆叠 */
@media (max-width: 860px) {
  .login-split {
    flex-direction: column;
    align-items: stretch;
    padding: 32px 20px;
    gap: 32px;
  }
  .intro-title {
    font-size: 26px;
  }
  .login-side {
    justify-content: center;
  }
  .login-card {
    width: 100%;
  }
}

@media (prefers-reduced-motion: reduce) {
  .bg-glow,
  .bg-word {
    animation: none;
  }
}
</style>
