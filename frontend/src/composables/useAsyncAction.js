import { ref } from 'vue'

/**
 * 异步动作封装：running / error + run(fn)。
 * 用于按钮触发的写操作，统一 loading 态与错误处理。
 * 错误通常已由 axios 拦截器弹出提示，此处只记录，不重复提示。
 */
export function useAsyncAction() {
  const running = ref(false)
  const error = ref('')

  async function run(fn) {
    running.value = true
    error.value = ''
    try {
      return await fn()
    } catch (e) {
      error.value = e?.response?.data?.detail || e?.message || '操作失败'
      throw e
    } finally {
      running.value = false
    }
  }

  return { running, error, run }
}
