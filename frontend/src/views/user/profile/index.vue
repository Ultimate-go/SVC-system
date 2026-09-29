<script setup>
/**
 * 个人中心。
 *
 * - 显示 /api/auth/me 的全部字段，重点 has_key 与 session_key 分开显示。
 * - 改自己的口令（PATCH /api/admin/users/{自己 id} 带 password）。
 * - 「刷新」→ /api/auth/me。
 * - 「退出登录」：先跳登录页，再弹后端返回的 notice。
 */
import { ref, reactive, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../../../stores/auth'
import { usersApi } from '../../../api/users'
import PageHeader from '../../../components/common/PageHeader.vue'
import KeyStatusTag from '../../../components/security/KeyStatusTag.vue'
import PubKeyFingerprint from '../../../components/security/PubKeyFingerprint.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'

const auth = useAuthStore()
const router = useRouter()

const loading = ref(false)
const pwdForm = reactive({ password: '', confirm: '' })
const changing = ref(false)
const changeTimings = ref(null)

const user = computed(() => auth.user)

async function refresh() {
  loading.value = true
  try {
    await auth.fetchMe()
    ElMessage.success('已刷新')
  } catch {
    // 401 已由拦截器兜底
  } finally {
    loading.value = false
  }
}

async function changePassword() {
  if (!user.value?.session_key) {
    ElMessage.warning('这次会话里没有你的私钥，请先重新登录再改口令')
    return
  }
  if (!pwdForm.password) {
    ElMessage.warning('请输入新口令')
    return
  }
  if (pwdForm.password !== pwdForm.confirm) {
    ElMessage.warning('两次口令不一致')
    return
  }
  changing.value = true
  changeTimings.value = null
  try {
    const { data } = await usersApi.patch(user.value.id, { password: pwdForm.password })
    changeTimings.value = data.timings
    ElMessage.success('口令已更改（私钥已重新封装）')
    pwdForm.password = ''
    pwdForm.confirm = ''
  } catch {
    // 403（没有私钥）等已由拦截器弹出
  } finally {
    changing.value = false
  }
}

async function logout() {
  try {
    await auth.logout()
    router.replace('/login')
  } catch {
    auth.clear()
    router.replace('/login')
  }
}

onMounted(() => {
  if (!user.value) auth.fetchMe().catch(() => {})
})
</script>

<template>
  <div>
    <PageHeader title="个人中心">
      <div class="actions">
        <el-button :loading="loading" @click="refresh">刷新</el-button>
        <el-button type="danger" @click="logout">退出登录</el-button>
      </div>
    </PageHeader>

    <div class="panel mb-3" v-if="user">
      <h4 class="sec-title">账户信息</h4>
      <div class="kv">
        <div class="kv-row"><span class="k">用户名</span><span class="mono">{{ user.username }}</span></div>
        <div class="kv-row"><span class="k">显示名</span><span>{{ user.display_name }}</span></div>
        <div class="kv-row"><span class="k">角色</span><span>{{ user.role === 'admin' ? '管理员' : '普通用户' }}</span></div>
        <div class="kv-row"><span class="k">密钥对</span><KeyStatusTag :user="user" /></div>
        <div class="kv-row"><span class="k">公钥指纹</span><PubKeyFingerprint :pub-key="user.pub_key" /></div>
      </div>
      <el-alert
        v-if="user.session_key === false"
        type="warning"
        :closable="false"
        class="mt-3"
        title="这次会话里没有私钥"
        description="重新登录就能解密。上传与验证不受影响。"
      />
    </div>

    <div class="panel">
      <h4 class="sec-title">改自己的口令</h4>
      <el-form label-width="90px" class="pwd-form" @submit.prevent>
        <el-form-item label="新口令">
          <el-input v-model="pwdForm.password" type="password" show-password />
        </el-form-item>
        <el-form-item label="确认口令">
          <el-input v-model="pwdForm.confirm" type="password" show-password />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="changing" @click="changePassword">改口令</el-button>
        </el-form-item>
      </el-form>
      <p class="note">改口令要重新封装私钥，会慢几百毫秒。</p>
      <StageTimeline v-if="changeTimings" :timings="changeTimings" class="mt-3" />
    </div>
  </div>
</template>

<style scoped>
.sec-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 14px;
}
.actions {
  display: flex;
  gap: 8px;
}
.kv {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.kv-row {
  display: flex;
  gap: 12px;
  align-items: baseline;
  font-size: 13px;
}
.kv-row .k {
  color: var(--text-3);
  width: 90px;
  flex-shrink: 0;
}
.pwd-form {
  max-width: 420px;
}
.note {
  font-size: 12px;
  color: var(--text-3);
}
</style>
