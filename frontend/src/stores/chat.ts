/**
 * 问答状态：消息列表 + SSE 流式接收（FR-01/02/04/05/09）。
 *
 * 契约依据：接口文档.md §4.3（提问）、§6（事件契约）、§7（联调约定）。
 *
 * 本 store 承担三件容易被写错的事：
 *
 *   1. **流式消息的终态由前端合成**。契约里 error 事件之后不会再有 done
 *      （§6 顺序表），因此收到 error 就必须立即定终态，不能等 done。
 *
 *   2. **所有 handler 按 localId 重新查找消息**，不闭包捕获消息对象。
 *      消息数组一旦被 loadHistory / reset 整体替换，闭包里的引用就变成孤儿，
 *      表现为「流还在追加，但界面上根本不更新」——这类 bug 极难定位。
 *
 *   3. **中断不是错误**。用户点「停止生成」时已收到的内容是有效结果，
 *      保留正文并标注「已停止生成」，而不是套一个红色错误框。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { ApiError, humanizeError, isApiError } from '@/api/http'
import {
  IMAGE_MAX_COUNT,
  QUESTION_MAX_LEN,
  listMessages,
  streamChat,
  type Citation,
  type ConfidenceLabel,
  type ImageEvent,
  type MessageItem,
  type Status,
} from '@/api/chat'
import { uploadImage, validateImage, type ImageUploadResponse } from '@/api/upload'
import { useSessionStore } from '@/stores/session'
import type { SseController } from '@/utils/sse'

/** 一条消息在界面上的生命周期阶段 */
export type StreamPhase = 'idle' | 'connecting' | 'streaming' | 'done' | 'error' | 'aborted'

/** 待发送 / 已上传的图片 */
export interface UploadedImage {
  localId: string
  fileName: string
  /** 本地预览用的 blob URL，组件卸载时需 revoke */
  previewUrl: string
  /** 上传成功后由后端返回 */
  imageId: string | null
  uploading: boolean
  error: string | null
}

/** 界面用的消息模型。字段覆盖后端 MessageItem，另加流式过程态。 */
export interface UiMessage {
  /** 前端稳定 key。不用数组下标：列表会整体替换，下标做 key 会导致渲染错位 */
  localId: string
  /** 历史消息由接口返回；流式新消息由 done 事件回填（后端 DoneEvent.qa_id） */
  qaId: number | null
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
  confidence: number | null
  confidenceLabel: ConfidenceLabel | null
  status: Status | null
  uncertain: string[]
  /** 用户提问时携带的图片 ID */
  imageIds: string[]
  /** 用户提问时的图片缩略图 */
  images: UploadedImage[]
  /** SSE image 事件：图片识别结果（识别在链路里做，不在上传接口） */
  imageResult: ImageEvent | null
  latencyMs: number | null
  createdAt: string
  traceId: string | null
  phase: StreamPhase
  /** 本条消息自身的失败原因，与页面级错误分开 */
  error: ApiError | null
  /** 已提交的反馈类型，提交后按钮进入只读态 */
  feedbackSubmitted: string | null
}

function newId(): string {
  // crypto.randomUUID 在 localhost 与 https 下均可用；老环境退化为时间戳+随机数
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function nowIso(): string {
  return new Date().toISOString()
}

/**
 * 会话级控制器。**刻意放在模块作用域且非响应式**：
 * 它是一个副作用句柄，不需要驱动渲染；放进 state 反而会被 Pinia 代理包装，
 * abort 时拿到的是 Proxy 而不是原对象。
 */
let controller: SseController | null = null

/** 组件卸载时中断在途的流，避免离开页面后仍在写状态 */
export function abortActiveStream(): void {
  controller?.abort()
}

export const useChatStore = defineStore('chat', () => {
  const sessionStore = useSessionStore()

  const messages = ref<UiMessage[]>([])
  const streaming = ref(false)
  const loadingHistory = ref(false)
  /** 页面级错误（历史加载失败等），与单条消息的 error 区分 */
  const pageError = ref<ApiError | null>(null)
  const pendingImages = ref<UploadedImage[]>([])
  /** 本轮的检索过滤条件（接口文档 §4.3） */
  const mode = ref<'qa' | 'training'>('qa')
  const deviceModel = ref<string>('')
  const category = ref<string>('')

  const canSend = computed(() => !streaming.value)
  const hasMessages = computed(() => messages.value.length > 0)
  const canAttachMore = computed(() => pendingImages.value.length < IMAGE_MAX_COUNT)

  /** 按 localId 查找。所有 SSE handler 都走这个函数，拿到的永远是当前数组里的对象。 */
  function findMessage(localId: string): UiMessage | undefined {
    return messages.value.find((item) => item.localId === localId)
  }

  // ------------------------------------------------------------------ 图片

  /** 选图 / 粘贴后加入待发送列表，并立即上传拿 image_id（两段式，接口文档 §4.2）。 */
  async function addImage(file: File): Promise<void> {
    if (!canAttachMore.value) {
      throw new ApiError({
        code: 'INVALID_ARGUMENT',
        message: `最多附带 ${IMAGE_MAX_COUNT} 张图片。`,
      })
    }

    await validateImage(file)

    const item: UploadedImage = {
      localId: newId(),
      fileName: file.name || 'screenshot.png',
      previewUrl: URL.createObjectURL(file),
      imageId: null,
      uploading: true,
      error: null,
    }
    pendingImages.value.push(item)

    try {
      const response: ImageUploadResponse = await uploadImage(file, sessionStore.currentSessionId)
      const target = pendingImages.value.find((img) => img.localId === item.localId)
      if (target) {
        target.imageId = response.image_id
        target.uploading = false
      }
    } catch (err) {
      const target = pendingImages.value.find((img) => img.localId === item.localId)
      if (target) {
        target.uploading = false
        target.error = humanizeError(err)
      }
      throw err
    }
  }

  function removeImage(localId: string): void {
    const index = pendingImages.value.findIndex((img) => img.localId === localId)
    if (index === -1) return
    URL.revokeObjectURL(pendingImages.value[index].previewUrl)
    pendingImages.value.splice(index, 1)
  }

  function clearImages(): void {
    for (const img of pendingImages.value) URL.revokeObjectURL(img.previewUrl)
    pendingImages.value = []
  }

  // ------------------------------------------------------------------ 提问

  /**
   * 发送提问并接收流式回答。
   *
   * 参数 question 显式传入而不是从输入框读，是为了支持「重试」——
   * 重试时输入框可能已被清空。
   */
  async function send(question: string): Promise<void> {
    const text = question.trim()
    if (!text) return
    if (streaming.value) return
    if (text.length > QUESTION_MAX_LEN) {
      throw new ApiError({
        code: 'QUESTION_TOO_LONG',
        message: `问题超过 ${QUESTION_MAX_LEN} 字上限。`,
      })
    }

    // 仅当全部图片上传完成才允许发送，否则会出现「问了但图没带上」
    if (pendingImages.value.some((img) => img.uploading)) {
      throw new ApiError({ code: 'INVALID_ARGUMENT', message: '图片仍在上传，请稍候。' })
    }
    const failed = pendingImages.value.filter((img) => !img.imageId)
    if (failed.length > 0) {
      throw new ApiError({ code: 'EMPTY_FILE', message: '有图片上传失败，请移除后重试。' })
    }

    const images = [...pendingImages.value]
    const imageIds = images.map((img) => img.imageId).filter((id): id is string => !!id)

    // 用户消息
    messages.value.push({
      localId: newId(),
      qaId: null,
      role: 'user',
      content: text,
      citations: [],
      confidence: null,
      confidenceLabel: null,
      status: null,
      uncertain: [],
      imageIds,
      images,
      imageResult: null,
      latencyMs: null,
      createdAt: nowIso(),
      traceId: null,
      phase: 'done',
      error: null,
      feedbackSubmitted: null,
    })

    // 助手占位消息：后续 token 增量全部累加到它的 content 上
    const assistantId = newId()
    messages.value.push({
      localId: assistantId,
      qaId: null,
      role: 'assistant',
      content: '',
      citations: [],
      confidence: null,
      confidenceLabel: null,
      status: null,
      uncertain: [],
      imageIds: [],
      images: [],
      imageResult: null,
      latencyMs: null,
      createdAt: nowIso(),
      traceId: null,
      phase: 'connecting',
      error: null,
      feedbackSubmitted: null,
    })

    // 图片已随本次提问带上，清空待发送区（blob URL 交由消息里的 images 持有）
    pendingImages.value = []

    streaming.value = true
    let startedAt = performance.now()

    controller = streamChat(
      {
        question: text,
        session_id: sessionStore.currentSessionId,
        image_ids: imageIds,
        mode: mode.value,
        device_model: deviceModel.value.trim() || null,
        category: category.value.trim() || null,
      },
      {
        onMeta: (data) => {
          const message = findMessage(assistantId)
          if (!message) return
          message.phase = 'streaming'
          message.traceId = data.trace_id
          startedAt = performance.now()
          // 后端可能新建了会话（提问时没带 session_id），这里回填
          if (data.session_id) sessionStore.setCurrent(data.session_id)
        },

        onToken: (data) => {
          const message = findMessage(assistantId)
          if (!message) return
          message.content += data.delta
          if (message.phase === 'connecting') message.phase = 'streaming'
        },

        onImage: (data) => {
          const message = findMessage(assistantId)
          if (message) message.imageResult = data
        },

        onCitations: (data) => {
          const message = findMessage(assistantId)
          if (message) message.citations = data.items
        },

        onDone: (data) => {
          const message = findMessage(assistantId)
          if (!message) return
          message.status = data.status
          message.confidence = data.confidence
          message.confidenceLabel = data.label
          message.uncertain = data.uncertain ?? []
          // done 事件带回本轮 qa_id，反馈按钮据此解锁（否则要刷新历史页才拿得到）
          message.qaId = data.qa_id ?? null
          message.latencyMs = Math.round(performance.now() - startedAt)
          // status=ERROR 是链路异常，按错误态渲染；NOT_COVERED 是正常拒答，不是错误
          message.phase = data.status === 'ERROR' ? 'error' : 'done'
          if (data.status === 'ERROR') {
            message.error = new ApiError({
              code: 'INTERNAL_ERROR',
              message: '链路返回异常状态 ERROR，请查看后端日志。',
            })
          }
        },

        onErrorEvent: (data) => {
          // 契约中 error 之后不会再有 done —— 这里立即定终态，不等 done
          const message = findMessage(assistantId)
          if (!message) return
          message.error = new ApiError({ code: data.code, message: data.message })
          message.status = 'ERROR'
          message.phase = 'error'
          message.latencyMs = Math.round(performance.now() - startedAt)
        },

        onAbort: () => {
          // 用户主动停止：内容保留，不套错误样式
          const message = findMessage(assistantId)
          if (message && (message.phase === 'streaming' || message.phase === 'connecting')) {
            message.phase = 'aborted'
          }
        },
      },
    )

    try {
      await controller.done
    } catch (err) {
      const message = findMessage(assistantId)
      if (message && (message.phase === 'connecting' || message.phase === 'streaming')) {
        message.phase = 'error'
        message.status = 'ERROR'
        message.error = isApiError(err)
          ? err
          : new ApiError({ code: 'INTERNAL_ERROR', message: humanizeError(err) })
      }
    } finally {
      const message = findMessage(assistantId)
      // 兜底：任何路径下都不允许占位消息停留在 connecting/streaming
      if (message && (message.phase === 'connecting' || message.phase === 'streaming')) {
        message.phase = 'done'
      }
      streaming.value = false
      controller = null
    }
  }

  /** 停止生成（接口文档 §7：读流可中断，已收内容保留）。 */
  function stop(): void {
    controller?.abort()
  }

  // -------------------------------------------------------------- 历史加载

  /** 把后端 MessageItem 映射成界面模型。 */
  function toUiMessage(item: MessageItem): UiMessage {
    return {
      localId: `qa-${item.qa_id}-${item.role}`,
      qaId: item.qa_id,
      role: item.role,
      content: item.content,
      citations: item.citations ?? [],
      confidence: item.confidence,
      confidenceLabel: item.confidence_label,
      status: item.status,
      uncertain: [],
      imageIds: item.image_ids ?? [],
      images: [],
      imageResult: null,
      latencyMs: item.latency_ms,
      createdAt: item.created_at,
      traceId: null,
      phase: item.status === 'ERROR' ? 'error' : 'done',
      error: null,
      feedbackSubmitted: null,
    }
  }

  /**
   * 载入某会话的历史消息（接口文档 §4.5）。
   *
   * 不合并 user/assistant：后端一轮问答本就返回两条记录，各自带 qa_id，
   * 前端再做合并反而要拆回去。qa_id 是历史消息提交反馈的唯一凭据。
   */
  async function loadHistory(sessionId: string): Promise<void> {
    loadingHistory.value = true
    pageError.value = null
    try {
      const response = await listMessages(sessionId, { with_citations: true, limit: 200 })
      messages.value = response.items.map(toUiMessage)
      sessionStore.setCurrent(response.session_id)
    } catch (err) {
      pageError.value = isApiError(err) ? err : null
      messages.value = []
      throw err
    } finally {
      loadingHistory.value = false
    }
  }

  /** 开启新会话：清空消息但保留过滤条件（通常是同一位工程师在连续排查）。 */
  function reset(): void {
    abortActiveStream()
    clearImages()
    messages.value = []
    pageError.value = null
    streaming.value = false
    sessionStore.startNew()
  }

  function clearPageError(): void {
    pageError.value = null
  }

  return {
    messages,
    streaming,
    loadingHistory,
    pageError,
    pendingImages,
    mode,
    deviceModel,
    category,
    canSend,
    hasMessages,
    canAttachMore,
    send,
    stop,
    addImage,
    removeImage,
    clearImages,
    loadHistory,
    reset,
    clearPageError,
  }
})
