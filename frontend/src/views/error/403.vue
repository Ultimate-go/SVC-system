<script setup>
import { useRouter } from 'vue-router'
import Icon from '../../components/icons/Icon.vue'
const router = useRouter()
</script>

<template>
  <div class="err-page">
    <el-result icon="warning" title="403" sub-title="需要管理员权限">
      <template #extra>
        <p class="human">权限由后端判定，前端只是不显示入口。如果你觉得这一页不该被拦，先看看账号角色。</p>
        <el-button type="primary" @click="router.replace('/')">
          <Icon name="back" :size="14" style="margin-right: 6px" />回首页
        </el-button>
      </template>
    </el-result>
  </div>
</template>

<style scoped>
.err-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100%;
}
.human {
  font-size: 13px;
  color: var(--text-2);
  max-width: 420px;
  margin: 0 auto 16px;
}
</style>
