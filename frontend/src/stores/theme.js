import { defineStore } from 'pinia'

const THEME_KEY = 'vds_theme'
const DENSITY_KEY = 'vds_density'
const MOTION_KEY = 'vds_motion'
const DETAIL_KEY = 'vds_detail'

/**
 * 主题 / 密度 / 动效 / 哈希详略。全部只存 localStorage，不影响后端行为。
 * 只是界面偏好。
 *
 * `detailMode` 置于此处而非各页自建 ref 的原因：
 *   它控制是否逐块显示指纹，该设置同时作用于文件详情、证据池、完整性验证三页。
 *   若按页存放，切换页面后会回到默认值。
 *   默认值：detailed。
 */
export const useThemeStore = defineStore('theme', {
  state: () => ({
    theme: localStorage.getItem(THEME_KEY) || 'dark',
    density: localStorage.getItem(DENSITY_KEY) || 'normal',
    motion: localStorage.getItem(MOTION_KEY) !== 'off',
    //: 'brief' | 'detail'
    detailMode: localStorage.getItem(DETAIL_KEY) === 'brief' ? 'brief' : 'detail',
  }),
  actions: {
    apply() {
      document.documentElement.setAttribute('data-theme', this.theme)
      document.documentElement.classList.toggle('dark', this.theme === 'dark')
      document.documentElement.classList.toggle('compact', this.density === 'compact')
      if (!this.motion) document.documentElement.classList.add('no-motion')
      else document.documentElement.classList.remove('no-motion')
    },
    setTheme(v) {
      this.theme = v
      localStorage.setItem(THEME_KEY, v)
      this.apply()
    },
    setDensity(v) {
      this.density = v
      localStorage.setItem(DENSITY_KEY, v)
      this.apply()
    },
    setMotion(v) {
      this.motion = v
      localStorage.setItem(MOTION_KEY, v ? 'on' : 'off')
      this.apply()
    },
    /**
     * 切"简略 / 详细"。
     *
      * 不操作 DOM：与上面三项不同，本项只控制渲染内容，
      *   由各页自行 `v-if`，无需给根元素挂 class。
     */
    setDetail(v) {
      this.detailMode = v === 'brief' ? 'brief' : 'detail'
      localStorage.setItem(DETAIL_KEY, this.detailMode)
    },
  },
})
