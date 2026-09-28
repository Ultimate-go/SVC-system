<script setup>
import { useRouter } from 'vue-router'
import Icon from '../../components/icons/Icon.vue'
const router = useRouter()
</script>

<template>
  <div class="err-page">
    <el-result icon="warning" title="403" sub-title="你没有访问这一页的权限（需要管理员）">
      <template #extra>
        <p class="human">前端不替你判断权限 —— 真正的门在路由 meta 与后端。若你认为这不该被拦，请检查账号角色。</p>
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
