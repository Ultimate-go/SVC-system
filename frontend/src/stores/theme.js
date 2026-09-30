import { defineStore } from 'pinia'

const THEME_KEY = 'vds_theme'
const DENSITY_KEY = 'vds_density'
const MOTION_KEY = 'vds_motion'
const DETAIL_KEY = 'vds_detail'

/**
 * 主题 / 密度 / 动效 / 哈希详略 —— 全部只存 localStorage，不影响后端任何行为。
 * 只是界面偏好。
 *
 * ★ “详略”（`detailMode`）为什么也放这里、而不是各页自己一个 ref：
 *   它管的是"要不要把**每一块的指纹**铺出来"，同一件事在文件详情、
 *   证据池、完整性验证三页都有。放在一页里切完、翻页又变回简略，
 *   是那种"每次都要重点一遍"的割裂感。默认给 **detailed**：
 *   这一版就是想让人看见密码学细节。
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
     * ★ 不动 DOM（与上面三个不同）：它只控制**渲染什么**，
     *   各页自己 `v-if` 就行，不需要给根元素挂 class。
     */
    setDetail(v) {
      this.detailMode = v === 'brief' ? 'brief' : 'detail'
      localStorage.setItem(DETAIL_KEY, this.detailMode)
    },
  },
})
