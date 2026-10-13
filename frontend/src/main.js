import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import { setUnauthorizedHandler } from './api/client'
import { startIdleWatch, idleExpired, touch } from './utils/session'
import { useAuthStore } from './stores/auth'
import { useThemeStore } from './stores/theme'

import './styles/index.css'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)
app.use(ElementPlus, { locale: zhCn })

// 主题初始化（读 localStorage，设置 data-theme 与 dark 类）。
const theme = useThemeStore(pinia)
theme.apply()

// 401 兜底处理：清除 token 并跳转登录。由 client.js 的拦截器调用。
//
// 两处约束：
//   1. 页面加载时可能并多个请求，同一时刻会出现多次 401 ——
//      只处理第一次，否则会重复跳转，并覆盖「会话失效」标记；
//   2. ``expired=1`` 供登录页判断是否显示「上次的登录状态已失效」。
//      仅在原本存在令牌时携带该标记。
let bouncePending = false
setUnauthorizedHandler(() => {
  const auth = useAuthStore(pinia)
  const wasLoggedIn = !!auth.token
  auth.clear()
  if (bouncePending) return
  const current = router.currentRoute.value
  if (current.name === 'login') return
  bouncePending = true
  const done = () => {
    bouncePending = false
  }
  router
    .replace({
      path: '/login',
      query: wasLoggedIn
        ? { redirect: current.fullPath, expired: '1' }
        : { redirect: current.fullPath },
    })
    .then(done, done)
})

// ---------------------------------------------------------------------------
// 会话时效：闲置 5 分钟才要求重新登录；刷新不影响登录态。
//
//   ① 启动时先看盘上的“最后活动时刻”：已经超时就当“已退出”处理 ——
//      免得一个放了很久的标签页看起来还在线；
//   ② 之后每 10 秒看一次；到点先请服务端把那枚令牌作废，再回登录页。
//      回落路径与 401 一致（expired=1，登录页会说明原因）。
// ---------------------------------------------------------------------------
const auth = useAuthStore(pinia)
if (auth.token && idleExpired()) auth.clear()
touch()

startIdleWatch(async () => {
  try {
    await auth.logout()
  } catch {
    auth.clear()
  }
  const current = router.currentRoute.value
  if (current.name === 'login') return
  router.replace({ path: '/login', query: { redirect: current.fullPath, expired: '1' } })
})

app.mount('#app')