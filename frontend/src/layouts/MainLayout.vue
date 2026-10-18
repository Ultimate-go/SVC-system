<script setup>
/**
 * 主布局。除了三块骨架，它只多做一件事：**账号第一次进来时**弹一次
 * 「本账号的加密参数」（见 components/security/AccountParamsDialog.vue）。
 *
 * 为什么放在布局层、而不是登录页：
 *   * 自助注册成功会**自动登录**并跳到首页；
 *   * 管理员新建的账号是**首次登录**时才进来。
 * 两条路都经过这里，所以提示只有一处实现。
 * 去重记录按账号存在浏览器里（utils/accountIntro.js），每个账号只弹一次。
 */
import { onMounted, ref, watch } from 'vue'
import AppSidebar from './components/AppSidebar.vue'
import AppTopbar from './components/AppTopbar.vue'
import TechBackdrop from './components/TechBackdrop.vue'
import AccountParamsDialog from '../components/security/AccountParamsDialog.vue'
import { useAuthStore } from '../stores/auth'
import { accountIntroSeen, markAccountIntroSeen } from '../utils/accountIntro'

const auth = useAuthStore()
const introOpen = ref(false)
const introUser = ref(null)

function maybeShowIntro() {
  const u = auth.user
  if (!u?.username) return
  if (accountIntroSeen(u.username)) return
  introUser.value = u
  introOpen.value = true
}

/** 弹窗关掉（任何一条关闭路径）就算看过 —— 记在浏览器里，下次不再弹。 */
function onIntroSeen() {
  markAccountIntroSeen(introUser.value?.username)
}

// `auth.user` 可能在本层挂载**之后**才到（刷新页面时 App.vue 的 /me 还在路上），
// 所以两处都看：挂载时看一次，账号变化时再看一次。
onMounted(maybeShowIntro)
watch(() => auth.user?.username, maybeShowIntro)
</script>

<template>
  <div class="main-layout">
    <TechBackdrop />
    <AppSidebar />
    <div class="body">
      <AppTopbar />
      <main class="content">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </main>
    </div>
    <AccountParamsDialog v-model="introOpen" :user="introUser" @seen="onIntroSeen" />
  </div>
</template>

<style scoped>
.main-layout {
  display: flex;
  height: 100%;
  position: relative;
}
.body {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  position: relative;
  z-index: 1;
}
.content {
  flex: 1;
  overflow-y: auto;
  padding: 20px;
}
</style>
