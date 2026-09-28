import { defineStore } from 'pinia'

const THEME_KEY = 'vds_theme'
const DENSITY_KEY = 'vds_density'
const MOTION_KEY = 'vds_motion'

/**
 * 主题 / 密度 / 动效 —— 全部只存 localStorage，不影响后端任何行为。
 * 只是界面偏好。
 */
export const useThemeStore = defineStore('theme', {
  state: () => ({
    theme: localStorage.getItem(THEME_KEY) || 'dark',
    density: localStorage.getItem(DENSITY_KEY) || 'normal',
    motion: localStorage.getItem(MOTION_KEY) !== 'off',
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
  },
})
