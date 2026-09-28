<script setup>
import { useRouter } from 'vue-router'
import Icon from '../../components/icons/Icon.vue'
const router = useRouter()
</script>

<template>
  <div class="err-page">
    <el-result icon="error" title="404" sub-title="页面不存在">
      <template #extra>
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
</style>
