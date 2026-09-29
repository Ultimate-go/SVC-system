import { ref } from 'vue'

/**
 * 异步动作封装：running / error + run(fn)。
 * 用于按钮点击触发的写操作，把 loading 态与错误统一起来。
 * 错误通常已由 axios 拦截器弹过 ElMessage，这里只记录，不重复弹。
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
