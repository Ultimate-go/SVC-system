import { ref, reactive, computed } from 'vue'

/**
 * 表格查询的通用封装：loading / error / 前端过滤 + 分页。
 *
 * 后端 GET /api/admin/users 没有任何过滤/分页参数，返回全量。
 * 「查询」在前端做（关键词 + 下拉过滤），分页也在前端做（默认每页 10 条）。
 * 不要假装后端支持查询参数。
 */
export function useTableQuery(fetchFn, { pageSize = 10, filterFn = null } = {}) {
  const loading = ref(false)
  const error = ref('')
  const rows = ref([])
  const page = ref(1)
  const filters = reactive({})

  const filtered = computed(() => {
    let out = rows.value
    if (filterFn) out = filterFn(out, filters)
    return out
  })

  const total = computed(() => filtered.value.length)
  const paged = computed(() => {
    const start = (page.value - 1) * pageSize
    return filtered.value.slice(start, start + pageSize)
  })

  async function load() {
    loading.value = true
    error.value = ''
    try {
      rows.value = await fetchFn()
      if (page.value > 1 && paged.value.length === 0) page.value = 1
    } catch (e) {
      error.value = e?.response?.data?.detail || e?.message || '加载失败'
    } finally {
      loading.value = false
    }
  }

  function setFilters(obj) {
    Object.assign(filters, obj)
    page.value = 1
  }

  return { loading, error, rows, filtered, total, paged, page, filters, load, setFilters }
}
