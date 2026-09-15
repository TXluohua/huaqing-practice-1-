/**
 * 会话列表状态（FR-06）。
 *
 * 负责两件事：
 *   1. 当前会话号 currentSessionId —— 提问时回传给 /api/chat/stream，
 *      为空则后端新建会话并在 meta 事件里回传（接口文档 §4.3）；
 *   2. 历史页的会话列表、关键字搜索与分页（接口文档 §4.4）。
 *
 * 之所以把「当前会话」放在会话 store 而非 chat store：
 * 历史页与问答页都要读写它，放在任一方都会形成反向依赖。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { isApiError, type ApiError } from '@/api/http'
import { listSessions, type SessionItem } from '@/api/chat'

export const useSessionStore = defineStore('session', () => {
  // ---------------------------------------------------------------- 当前会话
  const currentSessionId = ref<string | null>(null)
  /** 当前会话的标题，用于顶栏回显；流式新建会话时先用问题占位 */
  const currentTitle = ref<string>('')

  // ---------------------------------------------------------------- 列表状态
  const sessions = ref<SessionItem[]>([])
  const total = ref(0)
  const keyword = ref('')
  const limit = ref(20)
  const offset = ref(0)
  const loading = ref(false)
  /** 列表加载失败。视图层据此渲染 el-result，而不是渲染一个空列表 */
  const error = ref<ApiError | null>(null)

  const hasMore = computed(() => offset.value + sessions.value.length < total.value)
  const page = computed(() => Math.floor(offset.value / limit.value) + 1)
  const pageCount = computed(() => Math.max(1, Math.ceil(total.value / limit.value)))

  /** 选择/切换当前会话（历史页「继续对话」与流式 meta 事件都会调用）。 */
  function setCurrent(sessionId: string, title = ''): void {
    currentSessionId.value = sessionId
    if (title) currentTitle.value = title
  }

  /** 开新会话：清空当前会话号，后端会在下次提问时新建。 */
  function startNew(): void {
    currentSessionId.value = null
    currentTitle.value = ''
  }

  /** 拉取会话列表。失败时把 ApiError 收进 error，由视图层决定怎么展示。 */
  async function fetchSessions(options: { resetOffset?: boolean } = {}): Promise<void> {
    if (options.resetOffset) offset.value = 0

    loading.value = true
    error.value = null
    try {
      const response = await listSessions({
        limit: limit.value,
        offset: offset.value,
        keyword: keyword.value.trim() || undefined,
      })
      sessions.value = response.items
      total.value = response.total
    } catch (err) {
      error.value = isApiError(err) ? err : null
      sessions.value = []
      total.value = 0
      throw err
    } finally {
      loading.value = false
    }
  }

  /** 关键字搜索：重排名单回到第一页。 */
  async function search(value: string): Promise<void> {
    keyword.value = value
    await fetchSessions({ resetOffset: true })
  }

  async function goToPage(target: number): Promise<void> {
    offset.value = Math.max(0, (target - 1) * limit.value)
    await fetchSessions()
  }

  function reset(): void {
    startNew()
    sessions.value = []
    total.value = 0
    keyword.value = ''
    offset.value = 0
    error.value = null
  }

  return {
    currentSessionId,
    currentTitle,
    sessions,
    total,
    keyword,
    limit,
    offset,
    loading,
    error,
    hasMore,
    page,
    pageCount,
    setCurrent,
    startNew,
    fetchSessions,
    search,
    goToPage,
    reset,
  }
})
