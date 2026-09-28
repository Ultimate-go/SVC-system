<script setup>
/**
 * 用户详情 / 编辑。
 *
 * - 编辑 display_name / role / disabled。
 * - 公钥指纹用 PubKeyFingerprint。
 * - 底部警示：管理员改不了别人的口令（替他改会永久弄丢他的私钥）。
 */
import { ref, reactive, computed, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { usersApi } from '../../../api/users'
import { useAuthStore } from '../../../stores/auth'
import PageHeader from '../../../components/common/PageHeader.vue'
import PubKeyFingerprint from '../../../components/security/PubKeyFingerprint.vue'
import KeyStatusTag from '../../../components/security/KeyStatusTag.vue'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const id = computed(() => route.params.id)
const loading = ref(false)
const error = ref('')
const user = ref(null)
const form = reactive({ display_name: '', role: 'user', disabled: false })
const saving = ref(false)

const isSelf = computed(() => user.value?.username === auth.user?.username)

async function load() {
  loading.value = true
  error.value = ''
  try {
    const { data } = await usersApi.list()
    const found = data.find((u) => String(u.id) === String(id.value))
    if (!found) throw new Error('用户不存在')
    user.value = found
    form.display_name = found.display_name
    form.role = found.role
    form.disabled = found.disabled
  } catch (e) {
    error.value = e?.response?.data?.detail || e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    await usersApi.patch(id.value, {
      display_name: form.display_name,
      role: form.role,
      disabled: form.disabled,
    })
    ElMessage.success('已保存')
    await load()
  } catch {
    // 错误已由拦截器弹出
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <PageHeader title="用户详情" :subtitle="user ? `${user.username}（${user.display_name}）` : ''">
      <el-button @click="router.push('/users')">返回列表</el-button>
    </PageHeader>

    <div v-if="error" class="panel"><p class="text-danger">{{ error }}</p></div>
    <div v-else-if="user" class="detail">
      <div class="panel">
        <h4 class="sec-title">密钥对状态</h4>
        <KeyStatusTag :user="user" />
        <div class="kv mt-3">
          <div class="kv-row"><span class="k">公钥指纹</span><PubKeyFingerprint :pub-key="user.pub_key" /></div>
          <div class="kv-row"><span class="k">文件数</span><span class="v mono">{{ user.files }}</span></div>
        </div>
      </div>

      <div class="panel mt-3">
        <h4 class="sec-title">编辑</h4>
        <el-form :model="form" label-width="90px" @submit.prevent>
          <el-form-item label="显示名">
            <el-input v-model="form.display_name" />
          </el-form-item>
          <el-form-item label="角色">
            <el-radio-group v-model="form.role" :disabled="isSelf">
              <el-radio value="user">普通用户</el-radio>
              <el-radio value="admin">管理员</el-radio>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="停用">
            <el-switch v-model="form.disabled" :disabled="isSelf" />
          </el-form-item>
          <el-form-item>
            <el-button type="primary" :loading="saving" @click="save">保存</el-button>
          </el-form-item>
        </el-form>
      </div>

    </div>
  </div>
</template>

<style scoped>
.sec-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 14px;
}
.kv {
  display: flex;
  flex-direction: column;
  gap: 10px;
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
.kv-row .v {
  color: var(--text-1);
}
</style>
