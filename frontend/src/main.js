import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import { setUnauthorizedHandler } from './api/client'
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

// 401 兜底：清 token 并跳登录。由 client.js 的拦截器调用。
//
// ★ 两个细节都是真踩过的：
//   1. 页面一进来常常**并发**好几个请求，同一瞬间会来好几次 401 ——
//      只处理第一次，否则会重复跳转，还会把下面那个「为什么掉线」的标记冲掉；
//   2. ``expired=1`` 是给登录页看的，决定要不要显示「上次的登录状态已失效」。
//      只有**本来有令牌**才带这个标记：从没登录过的人不该看到任何提示。
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

app.mount('#app')
