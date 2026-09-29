<script setup>
/**
 * 添加用户页。
 *
 * 前端校验与后端一致：username 1-64 + 正则、display_name ≤64、role admin|user、password 1-256。
 * 提交后展示 timings（这一步做了「生成 SM2 密钥对 + 用口令封装私钥（20 万次 PBKDF2）」）。
 * 409（用户名已存在）原样提示。
 */
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { usersApi } from '../../../api/users'
import { validUsername, validDisplayName, validPassword } from '../../../utils/validate'
import PageHeader from '../../../components/common/PageHeader.vue'
import StageTimeline from '../../../components/security/StageTimeline.vue'

const router = useRouter()
const form = reactive({ username: '', display_name: '', role: 'user', password: '', confirm: '' })
const submitting = ref(false)
const timings = ref(null)

async function submit() {
  if (!validUsername(form.username)) {
    ElMessage.warning('用户名需 1-64 字符，只能含字母、数字、_ . -')
    return
  }
  if (!validDisplayName(form.display_name)) {
    ElMessage.warning('显示名不能超过 64 字符')
    return
  }
  if (!validPassword(form.password)) {
    ElMessage.warning('口令需 1-256 字符')
    return
  }
  if (form.password !== form.confirm) {
    ElMessage.warning('两次口令不一致')
    return
  }
  submitting.value = true
  timings.value = null
  try {
    const { data } = await usersApi.create({
      username: form.username,
      display_name: form.display_name,
      role: form.role,
      password: form.password,
    })
    timings.value = data.timings
    ElMessage.success('用户已创建，密钥对已生成')
  } catch {
    // 409 等错误已由拦截器弹出
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="add-user">
    <PageHeader title="添加用户" subtitle="建用户时生成密钥对；口令只用来包私钥，不存库">
      <el-button @click="router.push('/users')">返回列表</el-button>
    </PageHeader>

    <div class="panel form-panel">
      <el-form :model="form" label-width="90px" @submit.prevent>
        <el-form-item label="用户名">
          <el-input v-model="form.username" placeholder="字母 / 数字 / _ . -" />
        </el-form-item>
        <el-form-item label="显示名">
          <el-input v-model="form.display_name" placeholder="可选，默认同用户名" />
        </el-form-item>
        <el-form-item label="角色">
          <el-radio-group v-model="form.role">
            <el-radio value="user">普通用户</el-radio>
            <el-radio value="admin">管理员</el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="口令">
          <el-input v-model="form.password" type="password" show-password />
        </el-form-item>
        <el-form-item label="确认口令">
          <el-input v-model="form.confirm" type="password" show-password />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="submitting" @click="submit">创建</el-button>
        </el-form-item>
      </el-form>
    </div>

    <div v-if="timings" class="panel mt-3">
      <h4 class="sec-title">耗时（后端实测）</h4>
      <StageTimeline :timings="timings" />
      <p class="note text-2">
        生成 SM2 密钥对 + 用口令封装私钥（20 万次 PBKDF2），慢一点是正常的。
      </p>
    </div>
  </div>
</template>

<style scoped>
.form-panel {
  max-width: 560px;
}
.sec-title {
  font-size: 14px;
  font-weight: 500;
  margin-bottom: 10px;
}
.note {
  font-size: 12px;
  margin-top: 10px;
}
</style>
