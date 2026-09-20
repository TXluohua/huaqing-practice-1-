/**
 * 问答与会话接口。
 *
 * 契约依据：接口文档.md §4.3（SSE 提问）、§4.4（会话列表）、§4.5（历史消息）、§6（事件契约）。
 * 类型逐字段对齐 backend/schemas.py 与 backend/agents/state.py —— 三处同源，
 * 后端改字段时这里必须同步（开发文档 §6.3：state.py 属契约级文件）。
 */

import { request } from '@/api/http'
import { postSse, type ParsedSseEvent, type SseController } from '@/utils/sse'

// --------------------------------------------------------------------------- //
// 通用类型（接口文档 §3）
// --------------------------------------------------------------------------- //

/** 引用来源类型 / 引用项：**共享契约类型**，见 `@/api/contracts`（本模块 re-export 保持兼容） */
import type { Citation } from '@/api/contracts'

export type { Citation, SourceType } from '@/api/contracts'

/** 回答状态（接口文档 §3.3）：NOT_COVERED 为拒答，fail-closed */
export type Status = 'OK' | 'NOT_COVERED' | 'ERROR'

/** 置信度三级（接口文档 §3.2：>=0.80 high / 0.60-0.80 medium / <0.60 low） */
export type ConfidenceLabel = 'high' | 'medium' | 'low'

/** 问题最大长度（后端 backend/setting.py: question_max_len） */
export const QUESTION_MAX_LEN = 2000
/** 单次提问最多携带的图片数（接口文档 §4.3） */
export const IMAGE_MAX_COUNT = 3

// --------------------------------------------------------------------------- //
// 提问  POST /api/chat/stream
// --------------------------------------------------------------------------- //

export interface ChatRequest {
  question: string
  /** 缺省则新建会话，会话号在 meta 事件中回传 */
  session_id?: string | null
  /** 先经 /api/upload/image 获得的图片 ID，最多 3 个 */
  image_ids?: string[]
  /** qa 问答 / training 培训分层讲解（FR-09） */
  mode?: 'qa' | 'training'
  /** 设备型号，用于元数据过滤（FR-03） */
  device_model?: string | null
  /** 分类过滤：设备维护 / 工艺 / 标准 */
  category?: string | null
}

// ---- SSE 事件载荷（接口文档 §6）----

export interface MetaEvent {
  session_id: string
  trace_id: string
  ts: string
}

export interface ImageEvent {
  image_id: string
  /** alarm_screen / nameplate / …；unknown 表示未能识别 */
  image_type: string
  extracted: Record<string, unknown>
  confidence: number
}

export interface TokenEvent {
  /** 正文增量 */
  delta: string
}

export interface CitationsEvent {
  items: Citation[]
}

export interface DoneEvent {
  status: Status
  confidence: number
  label: ConfidenceLabel
  /** 未能溯源 / 存疑的点 */
  uncertain: string[]
  /**
   * 本轮问答记录 ID（后端 `DoneEvent.qa_id`，2026-09 追加）。
   * 拿到它才能对「刚生成的这条回答」直接提交反馈，不必刷新历史页。
   * 后端可能给 null（落库失败等），此时反馈按钮保持置灰。
   */
  qa_id: number | null
}

export interface ErrorEvent {
  code: string
  message: string
}

/**
 * 流式提问的事件回调集合。
 *
 * 注意 **error 事件的语义**：它是流已开始后的在流错误，收到即代表本次生成
 * 结束且**不会再有 done 事件**（接口文档 §6 的顺序表里 error 与 done 并列）。
 * 调用方需要在 onErrorEvent 中自行合成终态，不要等 done。
 */
export interface ChatSseHandlers {
  onMeta?(data: MetaEvent): void
  onImage?(data: ImageEvent): void
  onToken?(data: TokenEvent): void
  onCitations?(data: CitationsEvent): void
  onDone?(data: DoneEvent): void
  onErrorEvent?(data: ErrorEvent): void
  /** 用户主动中断（点击「停止生成」） */
  onAbort?(): void
  /** 流已建立 */
  onOpen?(): void
}

/** 事件名 → 载荷类型的映射表，仅用于把 unknown 断言成具体类型 */
function dispatch(handlers: ChatSseHandlers, event: ParsedSseEvent): void {
  switch (event.event) {
    case 'meta':
      handlers.onMeta?.(event.data as MetaEvent)
      break
    case 'image':
      handlers.onImage?.(event.data as ImageEvent)
      break
    case 'token':
      handlers.onToken?.(event.data as TokenEvent)
      break
    case 'citations':
      handlers.onCitations?.(event.data as CitationsEvent)
      break
    case 'done':
      handlers.onDone?.(event.data as DoneEvent)
      break
    case 'error':
      handlers.onErrorEvent?.(event.data as ErrorEvent)
      break
    default:
      // 未知事件（后端将来新增）在传输层已被保留，这里明确忽略并留痕
      console.warn('[sse] 收到未知事件，已忽略：', event.event)
  }
}

/**
 * 流式提问（接口文档 §4.3，核心接口）。
 *
 * 返回 SseController：`abort()` 停止生成，`done` 是本次生成的完成信号。
 * 详见 utils/sse.ts 对「流未开始 / 流已开始」两条错误路径的说明。
 */
export function streamChat(
  payload: ChatRequest,
  handlers: ChatSseHandlers,
  options: { signal?: AbortSignal } = {},
): SseController {
  return postSse(
    '/chat/stream',
    payload,
    {
      onEvent: (event) => dispatch(handlers, event),
      onAbort: () => handlers.onAbort?.(),
      onOpen: () => handlers.onOpen?.(),
    },
    { signal: options.signal },
  )
}

// --------------------------------------------------------------------------- //
// 会话  GET /api/chat/sessions
// --------------------------------------------------------------------------- //

export interface SessionItem {
  session_id: string
  thread_id: string
  title: string
  message_count: number
  last_question: string | null
  updated_at: string
}

export interface SessionListResponse {
  items: SessionItem[]
  total: number
  limit: number
  offset: number
  trace_id: string
}

export interface ListSessionsQuery {
  limit?: number
  offset?: number
  keyword?: string
}

/** 会话列表（接口文档 §4.4，FR-06）。 */
export function listSessions(query: ListSessionsQuery = {}): Promise<SessionListResponse> {
  return request<SessionListResponse>('/chat/sessions', { query: { ...query } })
}

// --------------------------------------------------------------------------- //
// 历史消息  GET /api/chat/sessions/{id}/messages
// --------------------------------------------------------------------------- //

/**
 * 一条历史消息。
 *
 * user 与 assistant 各一条，同属一个 qa_id。**不做两种行类型**：
 * user 行的 confidence / status / latency_ms 为 null，用同一结构 + 可空字段表达，
 * 与 backend/schemas.py 的 MessageItem 保持一致。
 */
export interface MessageItem {
  qa_id: number
  role: 'user' | 'assistant'
  content: string
  citations: Citation[]
  confidence: number | null
  confidence_label: ConfidenceLabel | null
  status: Status | null
  image_ids: string[]
  latency_ms: number | null
  created_at: string
}

export interface MessageListResponse {
  session_id: string
  thread_id: string
  items: MessageItem[]
  total: number
  limit: number
  offset: number
  trace_id: string
}

export interface ListMessagesQuery {
  limit?: number
  offset?: number
  with_citations?: boolean
}

/** 历史消息回看，含引用（接口文档 §4.5，FR-04 / FR-06）。会话不存在时抛 404 SESSION_NOT_FOUND。 */
export function listMessages(
  sessionId: string,
  query: ListMessagesQuery = {},
): Promise<MessageListResponse> {
  return request<MessageListResponse>(
    `/chat/sessions/${encodeURIComponent(sessionId)}/messages`,
    { query: { ...query } },
  )
}

