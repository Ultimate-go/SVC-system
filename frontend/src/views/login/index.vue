<script setup>
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
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
const introDone = ref(false)
const showDescription = ref(false)
const showForm = ref(false)
const brand = 'IAVC-VDSS'
let revealTimer
let descriptionTimer
let formTimer

/* ---------------------------------------------------------------------------
   登录等待动画
   ---------------------------------------------------------------------------
   登录的耗时**不在这个页面**，而在两段：
     ① 服务端校验口令、取回被封装的私钥（约 1~2 秒，多久不可知）；
     ② 浏览器里跑 20 万轮 PBKDF2-HMAC-SM3 解封私钥（约 2~3 秒，进度可算）。
   所以等待态也分两段演：网段用一条来回扫的光带（不确定进度），
   解封段用一条按**真实进度**从 0 长到 1 的光条 —— 百分比是真的，
   不是编个数字糊弄人（进度由 stores/auth.js → onUnlockProgress 报上来）。
   文案每 850ms 换一句，顺序与真实步骤对应。
--------------------------------------------------------------------------- */
const NET_TIPS = [
  '正在向服务端校验账号口令…',
  '正在取回被封装的私钥…',
  '正在核对密钥对归属…',
]
const KDF_TIP = '正在用口令派生 KEK…'
//: 上面那句的完整版本（面板宽度放不下，挂在 title 里）
const KDF_TIP_FULL = '正在用口令派生 KEK（PBKDF2-HMAC-SM3 · 20 万轮）…'
const UNWRAP_TIP = '正在解封私钥，建立加密会话…'
/** net（请求中）→ kdf（派生 KEK）→ unwrap（收尾）。 */
const busyPhase = ref('net')
const kdfPercent = ref(0)
const netTipIndex = ref(0)
let tipTimer

const busyTip = computed(() => {
  if (busyPhase.value === 'unwrap') return UNWRAP_TIP
  if (busyPhase.value === 'kdf') return KDF_TIP
  return NET_TIPS[netTipIndex.value % NET_TIPS.length]
})

/** 悬停时看到的那句 —— 面板宽度放不下算法参数，但丢掉又可惜。 */
const busyTipFull = computed(() => (busyPhase.value === 'kdf' ? KDF_TIP_FULL : busyTip.value))

/** 解封进度回调（真实进度）。到 1 就是 KEK 派生完了，换成收尾文案。 */
function onUnlockProgress(fraction) {
  const p = Math.max(0, Math.min(1, Number(fraction) || 0))
  kdfPercent.value = p
  busyPhase.value = p >= 1 ? 'unwrap' : 'kdf'
}

function stopTips() {
  if (tipTimer !== undefined) {
    window.clearInterval(tipTimer)
    tipTimer = undefined
  }
}

async function submit() {
  if (!form.username || !form.password) {
    ElMessage.warning('请输入账号与密码')
    return
  }
  loading.value = true
  busyPhase.value = 'net'
  kdfPercent.value = 0
  netTipIndex.value = 0
  stopTips()
  // 关掉动效的人不轮播文案：换行本身也是一种闪动。
  if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    tipTimer = window.setInterval(() => {
      netTipIndex.value = (netTipIndex.value + 1) % NET_TIPS.length
    }, 850)
  }
  try {
    await auth.login(form.username, form.password, { onUnlockProgress })
    const redirect = route.query.redirect || '/'
    router.replace(String(redirect))
  } catch {
    // 错误已由 axios 拦截器原样弹出（401/403/409 的中文理由）。
  } finally {
    stopTips()
    loading.value = false
  }
}

function startIntro() {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches
  if (reducedMotion) {
    introDone.value = true
    showDescription.value = true
    showForm.value = true
    return
  }

  revealTimer = window.setTimeout(() => {
    introDone.value = true
    descriptionTimer = window.setTimeout(() => {
      showDescription.value = true
      formTimer = window.setTimeout(() => {
        showForm.value = true
      }, 620)
    }, 760)
  }, brand.length * 105 + 260)
}

onMounted(startIntro)
onUnmounted(() => {
  window.clearTimeout(revealTimer)
  window.clearTimeout(descriptionTimer)
  window.clearTimeout(formTimer)
  stopTips()
})

const FLOATING_WORDS = ['SECURE', 'VERIFIABLE', 'INTEGRITY', 'COMMITMENT', 'CRYPTOGRAPHIC', 'DECENTRALIZED']
</script>

<template>
  <div class="login-page">
    <div class="backdrop" aria-hidden="true">
      <div class="bg-glow" />
      <div class="bg-grid" />
      <div class="scanline" />
      <VideoBackdrop />
      <span
        v-for="(word, index) in FLOATING_WORDS"
        :key="word"
        class="bg-word mono"
        :style="{ '--i': index, '--left': `${8 + index * 16}%`, '--delay': `${index * 1.1}s` }"
      >{{ word }}</span>
    </div>

    <main class="login-shell">
      <section class="brand-stage" :class="{ 'is-complete': introDone }" aria-label="IAVC-VDSS">
        <div class="terminal-line mono">
          <span class="prompt">root@vds:~$</span>
          <span class="command">./initialize --secure</span>
        </div>
        <h1 class="brand-title mono" aria-live="polite">
          <span
            v-for="(character, index) in brand.split('')"
            :key="`${character}-${index}`"
            class="brand-character"
            :style="{ '--char-index': index }"
          >{{ character }}</span><span class="cursor" aria-hidden="true" />
        </h1>
        <p class="brand-caption mono">INCREMENTAL AGGREGATION · VERIFIABLE STORAGE</p>
        <div class="progress-track" aria-hidden="true"><span /></div>
      </section>

      <section class="project-intro" :class="{ 'is-visible': showDescription }" aria-label="项目简介">
        <div class="intro-tag mono">VERIFIABLE · DECENTRALIZED · CRYPTOGRAPHIC</div>
        <h2>基于增量聚合向量承诺的<br />可验证分布式存储与查询系统</h2>
        <p>
          文件交给别人保管，凭什么放心？
          <br />答案不是“请相信我们”，而是一份谁都能自己验证的密码学证据。
        </p>
        <ul>
          <li><span class="intro-dot" />谁都能验证，只有所有者能解密</li>
          <li><span class="intro-dot" />不靠承诺，靠可验证</li>
          <li><span class="intro-dot" />证据恒为两个群元素（≤ 256 字节）</li>
        </ul>
      </section>

      <section class="login-panel" :class="{ 'is-visible': showForm }" aria-label="登录">
        <div class="panel-corner corner-top" />
        <div class="panel-corner corner-bottom" />
        <div class="panel-heading">
          <div class="status-dot" />
          <div>
            <p class="eyebrow mono">SECURE LOGIN / 01</p>
            <h2>登录系统</h2>
          </div>
        </div>
        <p class="card-sub">请输入账号和密码</p>

        <el-alert
          v-if="route.query.expired === '1'"
          type="info"
          :closable="false"
          class="login-alert"
          title="登录已过期，请重新登录"
          description="后端重启会换掉签名密钥，重登一次就好。"
        />

        <div class="form">
          <label class="field-label mono" for="login-username">账号</label>
          <el-input
            id="login-username"
            v-model="form.username"
            placeholder="请输入账号"
            size="large"
            :disabled="loading"
            autocomplete="username"
            @keyup.enter="submit"
          >
            <template #prefix><Icon name="user" :size="15" /></template>
          </el-input>
          <label class="field-label mono" for="login-password">密码</label>
          <el-input
            id="login-password"
            v-model="form.password"
            type="password"
            placeholder="请输入密码"
            size="large"
            show-password
            :disabled="loading"
            autocomplete="current-password"
            @keyup.enter="submit"
          >
            <template #prefix><Icon name="lock" :size="15" /></template>
          </el-input>
          <el-button type="primary" size="large" :loading="loading" class="submit" @click="submit">
            <span>{{ loading ? '正在登录' : '登录' }}</span>
            <span class="submit-arrow" aria-hidden="true">↗</span>
          </el-button>
        </div>

        <!-- 等待态：转圈 + 一行会换的提示 + 一条（解封阶段是真实进度的）光条 -->
        <transition name="busy">
          <div v-if="loading" class="login-busy" role="status" aria-live="polite">
            <span class="busy-ring" aria-hidden="true" />
            <div class="busy-main">
              <span :key="busyTip" class="busy-tip mono" :title="busyTipFull">{{ busyTip }}</span>
              <span
                class="busy-track"
                :class="busyPhase === 'net' ? 'is-flow' : 'is-bar'"
                aria-hidden="true"
              >
                <i :style="busyPhase === 'net' ? undefined : { transform: `scaleX(${kdfPercent})` }" />
              </span>
            </div>
            <span class="busy-percent mono">{{ busyPhase === 'net' ? '···' : `${Math.round(kdfPercent * 100)}%` }}</span>
          </div>
        </transition>

        <!-- 页脚在等待时**只变不可见、不移除** —— 它占的高度就是等待条的落点，
             抽掉它面板会回缩，等待条就会压到按钮上（踩过）。 -->
        <p class="panel-footer mono" :class="{ 'is-away': loading }"><span>●</span> END-TO-END VERIFICATION ENABLED</p>
      </section>
    </main>

    <footer class="system-footer mono"><span>SYS.STATUS</span> ONLINE <i /> NODE 01 · ENCRYPTED CHANNEL</footer>
  </div>
</template>

<style scoped>
.login-page {
  position: relative;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  background: #04070d;
  color: #e7f2ff;
}

.backdrop { position: absolute; inset: 0; overflow: hidden; pointer-events: none; }
.bg-glow {
  position: absolute;
  inset: -15%;
  background: radial-gradient(ellipse at 18% 38%, rgba(0, 214, 255, .16), transparent 42%), radial-gradient(ellipse at 82% 74%, rgba(88, 72, 255, .13), transparent 38%), radial-gradient(ellipse at 50% 15%, rgba(26, 255, 193, .06), transparent 30%);
  animation: glow-drift 14s ease-in-out infinite alternate;
}
.bg-grid {
  position: absolute;
  inset: 0;
  background-image: linear-gradient(rgba(106, 172, 226, .055) 1px, transparent 1px), linear-gradient(90deg, rgba(106, 172, 226, .055) 1px, transparent 1px);
  background-size: 54px 54px;
  mask-image: radial-gradient(ellipse 78% 70% at 50% 50%, #000 25%, transparent 100%);
}
.scanline { position: absolute; inset: 0; opacity: .22; background: repeating-linear-gradient(0deg, transparent 0 3px, rgba(255,255,255,.018) 4px); }
.bg-word { position: absolute; top: 0; left: var(--left); color: rgba(120, 190, 255, .14); font-size: 11px; letter-spacing: .28em; white-space: nowrap; animation: word-rise 18s linear infinite; animation-delay: var(--delay); }

.login-shell { position: relative; z-index: 1; width: min(1080px, 100%); min-height: 560px; display: grid; grid-template-columns: minmax(0, 1.15fr) minmax(330px, .85fr); align-items: center; gap: clamp(48px, 9vw, 120px); padding: 48px 32px; }
.brand-stage { position: absolute; z-index: 2; top: 50%; left: 50%; width: max-content; max-width: calc(100% - 32px); text-align: center; transform: translate(-50%, -50%); transition: opacity .6s ease, transform .8s cubic-bezier(.2,.8,.2,1); }
.brand-stage.is-complete { opacity: 0; transform: translate(-50%, calc(-50% - 150px)); pointer-events: none; }
.terminal-line { display: flex; gap: 12px; margin-bottom: 24px; color: rgba(183, 209, 231, .52); font-size: 11px; letter-spacing: .1em; }
.prompt { color: #44e4c0; }
.brand-title { display: flex; align-items: center; min-height: 1.2em; margin: 0; color: #f2f8ff; font-size: clamp(46px, 7vw, 82px); font-weight: 600; letter-spacing: .06em; line-height: 1; text-shadow: 0 0 30px rgba(0, 214, 255, .28); }
.brand-character { display: inline-block; opacity: 0; transform: translateY(12px); animation: type-in .36s cubic-bezier(.2,.8,.2,1) forwards; animation-delay: calc(var(--char-index) * 105ms); }
.cursor { width: 3px; height: .92em; margin-left: 9px; background: #42e4c2; box-shadow: 0 0 12px #42e4c2; animation: blink .85s steps(1) infinite; }
.is-complete .cursor { animation: blink .85s steps(1) infinite, cursor-fade .5s ease 1.1s forwards; }
.brand-caption { margin: 22px 0 28px; color: rgba(164, 196, 222, .64); font-size: 10px; letter-spacing: .23em; }
.progress-track { width: min(310px, 80%); height: 2px; background: rgba(143, 188, 220, .16); overflow: hidden; }
.progress-track span { display: block; width: 100%; height: 100%; transform-origin: left; background: linear-gradient(90deg, #29d7ff, #4be6bc); animation: progress 1.25s ease forwards; }
.project-intro { grid-column: 1; max-width: 560px; opacity: 0; transform: translateY(28px); transition: opacity .65s ease, transform .75s cubic-bezier(.2,.8,.2,1); }
.project-intro.is-visible { opacity: 1; transform: translateY(0); }
.intro-tag { margin-bottom: 16px; color: #47d9c1; font-size: 10px; letter-spacing: .18em; }
.project-intro h2 { margin: 0 0 16px; color: #e6edf7; font-size: clamp(22px, 3vw, 31px); font-weight: 500; line-height: 1.4; }
.project-intro p { margin: 0 0 19px; color: #9aadc1; font-size: 13px; line-height: 1.8; }
.project-intro ul { display: flex; flex-direction: column; gap: 9px; margin: 0; padding: 0; list-style: none; color: #cbd8e5; font-size: 13px; }
.project-intro li { display: flex; align-items: center; gap: 9px; }
.intro-dot { width: 6px; height: 6px; flex: 0 0 auto; border-radius: 50%; background: #43e4bf; box-shadow: 0 0 9px rgba(67, 228, 191, .75); }

.login-panel { position: relative; width: 100%; max-width: 390px; justify-self: end; padding: 34px 32px 26px; opacity: 0; transform: translateY(24px) scale(.98); background: rgba(7, 15, 27, .78); border: 1px solid rgba(104, 185, 228, .22); box-shadow: 0 24px 80px rgba(0,0,0,.42), inset 0 1px 0 rgba(255,255,255,.07); backdrop-filter: blur(16px); transition: opacity .7s ease, transform .75s cubic-bezier(.2,.8,.2,1); }
.login-panel.is-visible { opacity: 1; transform: translateY(0) scale(1); }
.panel-corner { position: absolute; width: 18px; height: 18px; border-color: #43dcbf; border-style: solid; }
.corner-top { top: -1px; right: -1px; border-width: 1px 1px 0 0; }
.corner-bottom { bottom: -1px; left: -1px; border-width: 0 0 1px 1px; }
.panel-heading { display: flex; align-items: center; gap: 13px; }
.status-dot { width: 8px; height: 8px; border-radius: 50%; background: #43e4bf; box-shadow: 0 0 0 4px rgba(67,228,191,.1), 0 0 14px #43e4bf; }
.eyebrow, .field-label { margin: 0; color: #47d9c1; font-size: 10px; letter-spacing: .16em; }
.panel-heading h2 { margin: 6px 0 0; font-size: 25px; font-weight: 500; letter-spacing: .04em; }
.card-sub { margin: 19px 0 26px; color: #8ca4bb; font-size: 13px; }
.login-alert { margin-bottom: 18px; }
.form { display: flex; flex-direction: column; gap: 10px; }
.field-label { margin-top: 5px; color: #7894ac; font-size: 9px; }
.submit { width: 100%; height: 44px; margin-top: 9px; border: 0; letter-spacing: .08em; }
.submit-arrow { margin-left: 12px; font-size: 17px; }

/* ---------- 登录等待态（参考“点进比赛”那种：转圈 + 一行会换的提示）---------- */
/* 绝对定位在面板底部、与 panel-footer 同一个位置 —— 这样等待态出现/消失时面板不会跳。 */
.login-busy {
  position: absolute; left: 32px; right: 32px; bottom: 14px;
  display: flex; align-items: center; gap: 10px; padding: 9px 11px;
  border: 1px solid rgba(67, 228, 191, .2); border-radius: 6px;
  background: linear-gradient(90deg, rgba(41, 215, 255, .08), rgba(67, 228, 191, .045));
  box-shadow: inset 0 1px 0 rgba(255, 255, 255, .04);
}
.busy-ring {
  position: relative; flex: 0 0 auto; width: 15px; height: 15px; border-radius: 50%;
  background: conic-gradient(from 0deg, rgba(67, 228, 191, 0) 0 18%, rgba(41, 215, 255, .6) 62%, #43e4bf 100%);
  -webkit-mask: radial-gradient(farthest-side, transparent calc(100% - 2px), #000 calc(100% - 1.6px));
  mask: radial-gradient(farthest-side, transparent calc(100% - 2px), #000 calc(100% - 1.6px));
  animation: busy-spin .85s linear infinite;
}
.busy-main { flex: 1 1 auto; min-width: 0; display: flex; flex-direction: column; gap: 7px; }
.busy-tip { display: block; overflow: hidden; color: #d3e8f6; font-size: 11px; line-height: 1.25; text-overflow: ellipsis; white-space: nowrap; animation: busy-in .32s cubic-bezier(.2, .8, .2, 1) both; }
.busy-track { position: relative; display: block; height: 2px; overflow: hidden; border-radius: 2px; background: rgba(143, 188, 220, .16); }
.busy-track i { position: absolute; inset: 0; display: block; }
/* 网段：不知道还要等多久 ⇒ 一条来回扫的光带（不确定进度） */
.busy-track.is-flow i { width: 44%; transform: translateX(-110%); background: linear-gradient(90deg, transparent, #29d7ff, #43e4bf, transparent); animation: busy-flow 1.35s cubic-bezier(.55, .08, .45, .92) infinite; }
/* 解封段：这就是派生 KEK 的**真实**进度（0→1），不是装饰性动画 */
.busy-track.is-bar i { transform: scaleX(0); transform-origin: left center; background: linear-gradient(90deg, #29d7ff, #43e4bf); box-shadow: 0 0 8px rgba(67, 228, 191, .55); }
.busy-percent { flex: 0 0 auto; min-width: 32px; color: #6f8ba0; font-size: 10px; text-align: right; }
.busy-enter-active, .busy-leave-active { transition: opacity .22s ease, transform .22s ease; }
.busy-enter-from, .busy-leave-to { opacity: 0; transform: translateY(-5px); }
.panel-footer { margin: 24px 0 0; color: rgba(139, 166, 187, .55); font-size: 9px; letter-spacing: .1em; }
/* 等待时留着位置、只隐掉（见模板里的注释） */
.panel-footer.is-away { visibility: hidden; }
.panel-footer span { color: #43e4bf; margin-right: 6px; }
.system-footer { position: absolute; z-index: 1; right: 30px; bottom: 22px; color: rgba(133, 163, 187, .5); font-size: 9px; letter-spacing: .12em; }
.system-footer span { color: #43dcbf; }
.system-footer i { display: inline-block; width: 4px; height: 4px; margin: 0 8px 2px; border-radius: 50%; background: #43e4bf; }

@keyframes type-in { to { opacity: 1; transform: translateY(0); } }
@keyframes blink { 50% { opacity: 0; } }
@keyframes cursor-fade { to { opacity: 0; } }
@keyframes progress { from { transform: scaleX(0); } to { transform: scaleX(1); } }
@keyframes busy-spin { to { transform: rotate(360deg); } }
/* 100% 必须是静止态（关动效时动画会停在最后一帧）：扫到头就停到轨道外面 */
@keyframes busy-flow { 0% { transform: translateX(-110%); } 100% { transform: translateX(260%); } }
@keyframes busy-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
@keyframes glow-drift { from { transform: translate(0, 0) scale(1); } to { transform: translate(2%, -2%) scale(1.06); } }
@keyframes word-rise { 0% { transform: translateY(105vh); opacity: 0; } 10%, 90% { opacity: 1; } 100% { transform: translateY(-10vh); opacity: 0; } }

@media (max-width: 760px) {
  .login-shell { min-height: 100vh; grid-template-columns: 1fr; gap: 34px; padding: 42px 22px 70px; align-content: center; }
  .brand-stage { top: 42%; }
  .brand-stage.is-complete { transform: translate(-50%, calc(-50% - 120px)); }
  .terminal-line { justify-content: center; margin-bottom: 20px; }
  .brand-title { justify-content: center; font-size: clamp(37px, 12vw, 58px); }
  .brand-caption { font-size: 8px; letter-spacing: .14em; }
  .progress-track { margin: 0 auto; }
  .project-intro { grid-column: 1; margin-top: 0; text-align: center; }
  .project-intro h2 { font-size: clamp(20px, 6vw, 27px); }
  .project-intro p, .project-intro ul { font-size: 12px; }
  .project-intro ul { align-items: center; }
  .login-panel { justify-self: center; max-width: 440px; padding: 29px 24px 23px; }
  .login-busy { left: 24px; right: 24px; bottom: 10px; }
  .system-footer { right: 0; bottom: 16px; width: 100%; text-align: center; font-size: 8px; }
}

@media (max-width: 390px) {
  .login-shell { padding-inline: 16px; }
  .brand-title { font-size: 34px; letter-spacing: .04em; }
  .terminal-line { font-size: 9px; gap: 8px; }
  .login-panel { padding-inline: 20px; }
  .login-busy { left: 20px; right: 20px; }
}

@media (prefers-reduced-motion: reduce) {
  .bg-glow, .bg-word, .brand-character, .cursor, .progress-track span,
  .busy-ring, .busy-tip, .busy-track.is-flow i { animation: none; }
  .brand-character { opacity: 1; transform: none; }
  .login-panel, .brand-stage, .project-intro { transition: none; }
  .brand-stage { position: relative; top: auto; left: auto; width: auto; max-width: none; transform: none; }
  .brand-stage.is-complete { opacity: 1; transform: none; }
  .project-intro { opacity: 1; transform: none; }
}
</style>
