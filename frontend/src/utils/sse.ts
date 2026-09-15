/**
 * SSE 传输层：fetch + ReadableStream 解析 text/event-stream。
 *
 * 契约依据：接口文档.md §6（事件契约）、§7（联调约定）。
 *
 * **为什么不用 EventSource**（接口文档 §5.5 明确禁止）：
 *   EventSource 只支持 GET，且无法携带自定义请求头。本系统的提问是 POST + JSON，
 *   还要带 X-User-Id / X-User-Role，EventSource 两个条件都不满足。
 *
 * 本文件只负责「字节 → 事件对象」的搬运，**不认识任何业务字段**：
 * 事件名到具体载荷类型（MetaEvent / TokenEvent / …）的映射在 api/chat.ts 完成。
 * 这样分层的好处是 utils/ 不依赖 api/ 的业务类型，缓冲区切分逻辑也能脱离 fetch 单测。
 */

import {
  ApiError,
  apiUrl,
  buildHeaders,
  isApiError,
  normalizeHttpError,
  toNetworkError,
} from '@/api/http'

/** 接口文档 §6 定义的六个事件名 */
export type SseEventName = 'meta' | 'image' | 'token' | 'citations' | 'done' | 'error'

/** 解析后的单个事件。data 为 JSON.parse 的结果；event 为服务端给的事件名。 */
export interface ParsedSseEvent {
  event: string
  data: unknown
  /** 原始文本，仅用于排障日志 */
  raw: string
}

// --------------------------------------------------------------------------- //
// 解析器（纯函数，不碰网络）
// --------------------------------------------------------------------------- //

export interface SseParser {
  /** 喂入一段文本，内部维持缓冲区并派发完整事件 */
  push(chunk: string): void
  /** 流结束：把残留缓冲区当最后一个事件处理（容忍服务端漏发结尾空行） */
  flush(): void
}

/**
 * 创建一个 SSE 解析器。
 *
 * 缓冲区分片规则（接口文档 §6「事件之间以空行分隔」）：
 *   1. 按 `\n\n` 切块，**最后一段留在缓冲区**——它可能是半截事件，
 *      直接解析会丢掉跨 chunk 的事件（这是 SSE 解析最常见的 bug）；
 *   2. CRLF 归一化为 LF，但只处理成对的 `\r\n`：若把裸 `\r` 也换掉，
 *      当 `\r` 恰好落在 chunk 末尾、`\n` 在下一个 chunk 开头时，
 *      会凭空造出一个假的事件边界；
 *   3. `:` 开头的行是注释/心跳，直接忽略（backend/setting.py 的
 *      sse_heartbeat_s=15 意味着长回答期间会收到心跳）；
 *   4. 多行 `data:` 按规范用 `\n` 连接，而不是拼接。
 *
 * 单个事件 JSON 解析失败只 warn 并丢弃该事件，**绝不中断整条流**：
 * 服务端将来新增事件类型时，老前端应当退化为忽略，而不是白屏。
 */
export function createSseParser(onEvent: (event: ParsedSseEvent) => void): SseParser {
  let buffer = ''

  const handleBlock = (block: string): void => {
    let eventName = ''
    const dataParts: string[] = []

    for (const line of block.split('\n')) {
      if (!line || line.startsWith(':')) continue // 空行 / 心跳注释
      const colon = line.indexOf(':')
      const field = colon === -1 ? line : line.slice(0, colon)
      let value = colon === -1 ? '' : line.slice(colon + 1)
      if (value.startsWith(' ')) value = value.slice(1) // 规范允许冒号后跟一个空格

      if (field === 'event') eventName = value.trim()
      else if (field === 'data') dataParts.push(value)
    }

    if (!eventName && dataParts.length === 0) return

    const dataText = dataParts.join('\n')
    let data: unknown = undefined
    if (dataText) {
      try {
        data = JSON.parse(dataText)
      } catch {
        console.warn('[sse] 事件 data 不是合法 JSON，已丢弃：', eventName, dataText.slice(0, 200))
        return
      }
    }

    onEvent({ event: eventName || 'message', data, raw: block })
  }

  return {
    push(chunk: string): void {
      buffer += chunk
      buffer = buffer.replace(/\r\n/g, '\n')

      let index = buffer.indexOf('\n\n')
      while (index !== -1) {
        const block = buffer.slice(0, index)
        buffer = buffer.slice(index + 2)
        if (block) handleBlock(block)
        index = buffer.indexOf('\n\n')
      }
    },
    flush(): void {
      const rest = buffer.trim()
      buffer = ''
      if (rest) handleBlock(rest)
    },
  }
}

// --------------------------------------------------------------------------- //
// 传输层
// --------------------------------------------------------------------------- //

export interface SseTransportHandlers {
  /** 每收到一个完整事件调用一次 */
  onEvent(event: ParsedSseEvent): void
  /** 流已建立（HTTP 200 + text/event-stream） */
  onOpen?(): void
  /** 用户主动中断。**不是错误**：已收到的内容视为有效结果 */
  onAbort?(): void
}

export interface SseTransportOptions {
  /** 外部中断信号（如组件卸载、点击「停止生成」） */
  signal?: AbortSignal
  /** 空闲超时：只要在收数据就重置，心跳同样算存活。接口文档 §7 规定读流超时 60s */
  idleTimeoutMs?: number
  /** 硬上限：兜住「心跳不断但正文永远不来」的病态情况 */
  hardTimeoutMs?: number
}

export interface SseController {
  /** 中断本次生成；已收到的内容保留 */
  abort(): void
  /**
   * 完成信号：
   *   - 正常结束 / 用户中断 → resolve（半截答案是合法结果）
   *   - 流未开始就失败、网络中断、超时 → reject(ApiError)
   */
  done: Promise<void>
}

const DEFAULT_IDLE_TIMEOUT_MS = 60_000
const DEFAULT_HARD_TIMEOUT_MS = 180_000

/**
 * POST 一个 JSON 请求体并以 SSE 方式读取响应。
 *
 * **两条错误路径必须分开处理**（接口文档 §7 最后一行）：
 *
 *   流未开始（HTTP 非 2xx）→ 走 normalizeHttpError，抛出统一错误体。
 *     典型场景：后端未启动时 Vite 代理返回空 500，归为 NETWORK_ERROR。
 *
 *   流已开始（HTTP 200 之后）→ 错误以 `error` 事件的形式在流内到达，
 *     **不抛异常**，交给调用方的 onEvent 处理。契约中 error 之后不会再有
 *     done 事件，因此收到 error 即视为流终结，这里会主动取消读取。
 */
export function postSse<TBody>(
  path: string,
  body: TBody,
  handlers: SseTransportHandlers,
  options: SseTransportOptions = {},
): SseController {
  const {
    signal,
    idleTimeoutMs = DEFAULT_IDLE_TIMEOUT_MS,
    hardTimeoutMs = DEFAULT_HARD_TIMEOUT_MS,
  } = options

  const controller = new AbortController()
  /** 区分「用户点的停止」与「超时中止」——两者都表现为 AbortError，但语义完全相反 */
  let abortedByUser = false
  let timedOut = false
  let reader: ReadableStreamDefaultReader<Uint8Array> | null = null
  let idleTimer: number | undefined
  let hardTimer: number | undefined

  const onExternalAbort = (): void => {
    abortedByUser = true
    controller.abort()
  }
  signal?.addEventListener('abort', onExternalAbort, { once: true })

  const clearTimers = (): void => {
    window.clearTimeout(idleTimer)
    window.clearTimeout(hardTimer)
  }

  /** 每次收到数据就重置空闲计时，因此心跳（空注释行）也能维持连接 */
  const armIdleTimer = (): void => {
    window.clearTimeout(idleTimer)
    idleTimer = window.setTimeout(() => {
      timedOut = true
      controller.abort()
    }, idleTimeoutMs)
  }

  const done = (async (): Promise<void> => {
    // 收到 done / error 事件即视为流终结：主动断开，不等服务端关连接
    let finished = false
    const parser = createSseParser((event) => {
      handlers.onEvent(event)
      if (event.event === 'done' || event.event === 'error') finished = true
    })

    try {
      let response: Response
      try {
        response = await fetch(apiUrl(path), {
          method: 'POST',
          headers: buildHeaders({
            'Content-Type': 'application/json',
            Accept: 'text/event-stream',
          }),
          body: JSON.stringify(body),
          signal: controller.signal,
          // 让浏览器把响应体当流处理（部分环境下可避免整体缓冲）
          cache: 'no-store',
        })
      } catch (error) {
        if (abortedByUser) {
          handlers.onAbort?.()
          return
        }
        if (timedOut) {
          throw new ApiError({ code: 'STREAM_TIMEOUT', message: '建立连接超时，请稍后重试。' })
        }
        throw toNetworkError(error)
      }

      if (!response.ok) throw await normalizeHttpError(response)

      if (!response.body) {
        throw new ApiError({
          status: response.status,
          code: 'INVALID_RESPONSE',
          message: '响应没有可读流，无法以 SSE 方式解析。',
        })
      }

      // 契约校验：成功响应必须是 text/event-stream。若返回 JSON 却按 SSE 解析，
      // 结果是一个事件都没有、界面表现为「回答为空」，比直接报错更难排查。
      const contentType = response.headers.get('content-type') ?? ''
      if (!contentType.includes('text/event-stream')) {
        throw new ApiError({
          status: response.status,
          code: 'INVALID_RESPONSE',
          message: `期望 SSE 流，实际 Content-Type 为 ${contentType || '（空）'}，接口契约可能已变更。`,
        })
      }

      handlers.onOpen?.()
      armIdleTimer()
      hardTimer = window.setTimeout(() => {
        timedOut = true
        controller.abort()
      }, hardTimeoutMs)

      reader = response.body.getReader()
      const decoder = new TextDecoder('utf-8')

      while (!finished) {
        const { done: streamEnded, value } = await reader.read()
        if (streamEnded) break
        armIdleTimer()
        // {stream:true} 是必须的：中文一个字符占 3 字节，可能被 chunk 边界切开，
        // 不带该选项会解码出乱码（U+FFFD）
        parser.push(decoder.decode(value, { stream: true }))
      }

      // 冲刷解码器与解析器的残留：服务端可能漏发结尾空行
      parser.push(decoder.decode())
      parser.flush()
    } catch (error) {
      if (abortedByUser) {
        handlers.onAbort?.()
        return
      }
      if (timedOut) {
        throw new ApiError({
          code: 'STREAM_TIMEOUT',
          message: '回答超时：长时间未收到新内容，已中断本次生成。',
        })
      }
      if (isApiError(error)) throw error
      throw toNetworkError(error)
    } finally {
      clearTimers()
      try {
        await reader?.cancel()
      } catch {
        // 流可能已自然结束，cancel 会抛 —— 忽略
      }
      reader?.releaseLock()
      reader = null
      signal?.removeEventListener('abort', onExternalAbort)
    }
  })()

  return {
    abort(): void {
      abortedByUser = true
      controller.abort()
      // abort 未必立刻打断已挂起的 read()，显式 cancel 一次更稳
      void reader?.cancel().catch(() => undefined)
    },
    done,
  }
}
