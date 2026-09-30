<script setup>
/**
 * 界面偏好：主题（深/浅）、表格密度（松/紧）、是否开启动效。全部只存 localStorage。
 */
import { computed } from 'vue'
import { useThemeStore } from '../../../stores/theme'
import PageHeader from '../../../components/common/PageHeader.vue'

const theme = useThemeStore()

const isDark = computed(() => theme.theme === 'dark')
</script>

<template>
  <div>
    <PageHeader title="界面偏好" subtitle="只管界面，不动后端" />

    <el-alert type="info" :closable="false" class="mb-3" title="偏好存在浏览器本地，换台机器就没了。" />

    <div class="panel">
      <div class="setting-row">
        <div>
          <div class="name">主题</div>
          <div class="desc">深色为默认（答辩投影 + 演示气质）</div>
        </div>
        <el-switch
          :model-value="isDark"
          active-text="深色"
          inactive-text="浅色"
          @change="(v) => theme.setTheme(v ? 'dark' : 'light')"
        />
      </div>

      <div class="setting-row">
        <div>
          <div class="name">表格密度</div>
          <div class="desc">紧 = 行高更小，适合看更多数据</div>
        </div>
        <el-radio-group :model-value="theme.density" @change="(v) => theme.setDensity(v)">
          <el-radio-button value="normal">松</el-radio-button>
          <el-radio-button value="compact">紧</el-radio-button>
        </el-radio-group>
      </div>

      <div class="setting-row">
        <div>
          <div class="name">动效</div>
          <div class="desc">关闭后所有过渡动画几乎消失（prefers-reduced-motion 也会自动关）</div>
        </div>
        <el-switch :model-value="theme.motion" active-text="开" inactive-text="关" @change="(v) => theme.setMotion(v)" />
      </div>

      <div class="setting-row">
        <div>
          <div class="name">哈希详略</div>
          <div class="desc">
            详细 = 把每一块的分量指纹铺出来（停在指纹上显示完整十六进制）；
            简略只给块号、下标与结论。文件详情 / 证据池 / 完整性验证三页共用这一个偏好
          </div>
        </div>
        <el-radio-group :model-value="theme.detailMode" @change="(v) => theme.setDetail(v)">
          <el-radio-button value="brief">简略</el-radio-button>
          <el-radio-button value="detail">详细</el-radio-button>
        </el-radio-group>
      </div>
    </div>
  </div>
</template>

<style scoped>
.setting-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px 0;
  border-bottom: 1px solid var(--line);
}
.setting-row:last-child {
  border-bottom: none;
}
.name {
  font-size: 14px;
  font-weight: 500;
}
.desc {
  font-size: 12px;
  color: var(--text-3);
  margin-top: 2px;
}
</style>
