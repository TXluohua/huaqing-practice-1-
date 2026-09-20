/**
 * HTTP 基础设施：请求头、错误归一化、统一 JSON 请求。
 *
 * 契约依据：接口文档.md
 *   §1.2 —— 成功响应**直接返回业务数据对象**，不套 {code,data} 信封。
 *           这正是 request<T>() 直接返回 T 而不做任何解包的原因：
 *           前端没有 "unwrap" 这一步，业务类型就是响应类型。
 *   §1.3 —— X-User-Id / X-User-Role 透传到检索层（检索即鉴权）。
 *   §5   —— 失败 = HTTP 状态码 + 统一错误体 {code,message,detail?,trace_id}。
 *
 * 为什么不用 axios：utils/sse.ts 必须用原生 fetch（要读 ReadableStream），
 * 若 JSON 走 axios 就会出现两套请求头、两套错误处理。统一用 fetch 后，
 * 错误归一化函数只有 normalizeHttpError 这一份，JSON 路径与 SSE 预流路径共用。
 */

/** 接口基础路径（接口文档 §1.2：本期不引入 /v1 前缀） */
export const API_BASE = '/api'

/** 匿名身份缺省值（接口文档 §1.3） */
export const DEFAULT_USER_ID = 'anonymous'
export const DEFAULT_USER_ROLE = 'engineer'

/** 角色取值：engineer / admin（接口文档 §1.3） */
export type UserRole = 'engineer' | 'admin'

/** 未指定超时的普通请求上限 */
const DEFAULT_TIMEOUT_MS = 30_000

// --------------------------------------------------------------------------- //
// 错误类型
// --------------------------------------------------------------------------- //

export interface ApiErrorInit {
  /** HTTP 状态码；网络层失败时为 0 */
  status?: number
  /** 业务错误码，取值见接口文档 §5；网络层失败时为 NETWORK_ERROR */
  code: string
  message: string
  detail?: Record<string, unknown> | null
  traceId?: string | null
  /** true 表示请求根本没到达后端（后端未启动 / 代理 ECONNREFUSED） */
  isNetwork?: boolean
}

/**
 * 统一接口异常。
 *
 * isNetwork 用来区分两类失败，这个区分很重要：
 *   - 业务报错（404 SESSION_NOT_FOUND 等）：是「服务在，但这次请求有问题」，可提示用户重试或改输入；
 *   - 网络失败：是「服务不在」，应该提示「后端未启动」并给出启动命令。
 * 混在一起时，用户看到的是「请求失败」，排障时最容易被误导。
 */
export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly detail: Record<string, unknown> | null
  readonly traceId: string | null
  readonly isNetwork: boolean

  constructor(init: ApiErrorInit) {
    super(init.message)
    this.name = 'ApiError'
    this.status = init.status ?? 0
    this.code = init.code
    this.detail = init.detail ?? null
    this.traceId = init.traceId ?? null
    this.isNetwork = init.isNetwork ?? false
  }
}

export function isApiError(value: unknown): value is ApiError {
  return value instanceof ApiError
}

/** 后端未启动时的统一文案（Vite 代理会返回 500 且 body 为空，无法从 body 中获取信息） */
const BACKEND_UNREACHABLE =
  '后端服务不可达：请先启动 FastAPI（uvicorn backend.main:app --reload，端口 8000）后重试。'

/**
 * 把非 2xx 响应归一化成 ApiError。**唯一**的错误解析实现。
 *
 * 分三种情况（接口文档 §5 + §7 最后一行）：
 *   1. body 是统一错误体 → 取 code / message / detail / trace_id；
 *   2. body 不是 JSON（Vite 代理在后端未启动时返回空 500，nginx 可能返回 HTML）
 *      → 归为 NETWORK_ERROR，附「后端不可达」文案；
 *   3. JSON 但不符合错误体形状 → 保留状态码，code 用 HTTP_<status>。
 */
export async function normalizeHttpError(res: Response): Promise<ApiError> {
  let text = ''
  try {
    text = await res.text()
  } catch {
    text = ''
  }

  if (text) {
    try {
      const body = JSON.parse(text) as Record<string, unknown>
      const code = typeof body.code === 'string' ? body.code : `HTTP_${res.status}`
      const message = typeof body.message === 'string' ? body.message : `请求失败（HTTP ${res.status}）`
      return new ApiError({
        status: res.status,
        code,
        message,
        detail: (body.detail as Record<string, unknown> | undefined) ?? null,
        traceId: typeof body.trace_id === 'string' ? body.trace_id : null,
      })
    } catch {
      // 不是 JSON —— 落到下面的兜底分支
    }
  }

  // 空 body / 非 JSON：代理层失败。502/503/504 与空 500 都归到这里。
  return new ApiError({
    status: res.status,
    code: 'NETWORK_ERROR',
    message: BACKEND_UNREACHABLE,
    isNetwork: true,
  })
}

/** fetch 直接抛 TypeError（Failed to fetch / 连接被拒）时的归一化入口。 */
export function toNetworkError(cause: unknown): ApiError {
  if (isApiError(cause)) return cause
  return new ApiError({
    code: 'NETWORK_ERROR',
    message: BACKEND_UNREACHABLE,
    detail: { cause: cause instanceof Error ? cause.message : String(cause) },
    isNetwork: true,
  })
}

/** 错误码 → 中文提示。接口文档 §5 的错误码表全覆盖，未列出的回退到后端 message。 */
const ERROR_MESSAGES: Record<string, string> = {
  NETWORK_ERROR: BACKEND_UNREACHABLE,
  TIMEOUT: '请求超时，请稍后重试。',
  STREAM_TIMEOUT: '回答超时：长时间未收到新内容，已中断本次生成。',
  INVALID_ARGUMENT: '请求参数不合法，请检查输入。',
  EMPTY_FILE: '文件为空，请重新选择。',
  IMAGE_TOO_SMALL: '图片过小（最小边需 200px），识别质量不可用。',
  IMAGE_TOO_LARGE: '图片超过 5MB 限制。',
  UNSUPPORTED_IMAGE_TYPE: '图片格式不支持，仅允许 jpg / jpeg / png / webp。',
  UNSUPPORTED_DOC_TYPE: '文档格式不支持。',
  SESSION_NOT_FOUND: '会话不存在或已被删除。',
  QA_NOT_FOUND: '问答记录不存在，无法提交反馈。',
  JOB_NOT_FOUND: '任务不存在或已过期。',
  DOC_VERSION_EXISTS: '该文档的此版本已存在。',
  CORRECTED_ANSWER_REQUIRED: '反馈类型为「修正」时必须填写修正内容。',
  QUESTION_TOO_LONG: '问题超过 2000 字上限。',
  RATE_LIMITED: '请求过于频繁（并发上限 20），请稍后重试。',
  VECTOR_STORE_UNAVAILABLE: '向量库不可用，检索链路已中断。',
  LLM_UNAVAILABLE: '文本模型不可用，暂时无法生成回答。',
  SERVICE_NOT_READY: '该功能暂不可用：后端服务未就绪，请稍后重试。',
  DB_UNAVAILABLE: '数据库不可用，该功能暂时无法使用。',
  KB_UNAVAILABLE: '知识库暂不可用，请稍后重试。',
  INTERNAL_ERROR: '服务内部错误，请查看后端日志。',

  // ---- 业务功能（接口文档 §10）：维护计划 / 备件商城与采购 / 考核认证 ----
  // 后端 message 已经是中文人话，这里只作兜底（humanizeError 优先用后端 message）
  PLAN_NOT_FOUND: '维护计划项不存在，请刷新列表。',
  PART_NOT_FOUND: '备件不存在：编码或名称有误。',
  SUBSTITUTE_BASIS_REQUIRED: '替代件缺少兼容性依据，需原厂确认后才能采购。',
  ORDER_NOT_FOUND: '采购申请单不存在，请刷新列表。',
  ITEM_NOT_FOUND: '购物车里没有该备件，请刷新后重试。',
  EMPTY_ORDER: '购物车为空，请先加入备件再提交。',
  REASON_REQUIRED: '驳回或撤销必须填写原因。',
  INVALID_STATE: '订单状态已变更，请刷新后重试。',
  ALREADY_SETTLED: '该订单已登记结算，请勿重复提交。',
  QUIZ_GENERATION_FAILED: '该主题抽不到有依据的题目，请换个主题或设备。',
  QUIZ_EMPTY: '这份试卷没有题目，请重新出题。',
  QUIZ_NOT_FOUND: '试卷不存在，请重新出题。',
  ATTEMPT_NOT_PASSED: '认证必须基于一次通过的考核。',
  ATTEMPT_NOT_FOUND: '考核记录不存在，请重新考试。',
}

/** 把 ApiError 转成可直接展示给用户的一句话。 */
export function humanizeError(error: unknown): string {
  if (!isApiError(error)) {
    return error instanceof Error ? error.message : String(error)
  }
  // 后端 message 通常比本地映射更具体（如「图片超过 5MB 限制」带实际值），
  // 因此仅在本地表命中且后端未给 message 时才用本地文案。
  if (!error.isNetwork && error.message) return error.message
  return ERROR_MESSAGES[error.code] ?? error.message
}

// --------------------------------------------------------------------------- //
// 身份（接口文档 §1.3：本期最简实现，不做签名校验）
// --------------------------------------------------------------------------- //

const USER_ID_KEY = 'smka.user_id'
const USER_ROLE_KEY = 'smka.user_role'

export function getUserId(): string {
  return localStorage.getItem(USER_ID_KEY) || DEFAULT_USER_ID
}

export function setUserId(value: string): void {
  const trimmed = value.trim()
  if (trimmed) localStorage.setItem(USER_ID_KEY, trimmed)
  else localStorage.removeItem(USER_ID_KEY)
}

export function getUserRole(): UserRole {
  return localStorage.getItem(USER_ROLE_KEY) === 'admin' ? 'admin' : DEFAULT_USER_ROLE
}

export function setUserRole(value: UserRole): void {
  localStorage.setItem(USER_ROLE_KEY, value)
}

/**
 * 统一请求头。FormData 不设 Content-Type —— 必须让浏览器自己带 boundary，
 * 手写 multipart/form-data 会因缺少 boundary 而被后端拒绝。
 */
export function buildHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'X-User-Id': getUserId(),
    'X-User-Role': getUserRole(),
  }
  return { ...headers, ...extra }
}

// --------------------------------------------------------------------------- //
// URL 与时间
// --------------------------------------------------------------------------- //

/** 拼查询串：丢弃 undefined / null / 空串，避免出现 ?keyword= 这种空参。 */
export function toQuery(params?: Record<string, unknown>): string {
  if (!params) return ''
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    search.append(key, String(value))
  }
  const qs = search.toString()
  return qs ? `?${qs}` : ''
}

/** 拼出带查询参数的完整接口地址（sse.ts 复用同一套拼装，避免两处不一致）。 */
export function apiUrl(path: string, params?: Record<string, unknown>): string {
  return `${API_BASE}${path}${toQuery(params)}`
}

/** ISO 8601 → 本地可读时间。不引 dayjs，Intl 足够。 */
export function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}

// --------------------------------------------------------------------------- //
// 统一请求
// --------------------------------------------------------------------------- //

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  query?: Record<string, unknown>
  /** JSON 请求体 */
  json?: unknown
  /** multipart 请求体，与 json 互斥 */
  formData?: FormData
  signal?: AbortSignal
  timeoutMs?: number
}

/**
 * 发一次 JSON / multipart 请求并返回业务对象。
 *
 * 注意返回类型是 T 本身，不是 {data:T} —— 见文件头对接口文档 §1.2 的说明。
 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', query, json, formData, signal, timeoutMs = DEFAULT_TIMEOUT_MS } = options

  const controller = new AbortController()
  // 外部 signal（如组件卸载）与内部超时共用一个 controller：任一触发都中止请求
  const onExternalAbort = () => controller.abort()
  signal?.addEventListener('abort', onExternalAbort, { once: true })
  const timer = window.setTimeout(() => controller.abort(), timeoutMs)

  let response: Response
  try {
    const init: RequestInit = {
      method,
      headers: buildHeaders(formData ? undefined : json !== undefined ? { 'Content-Type': 'application/json' } : undefined),
      signal: controller.signal,
    }
    if (formData) init.body = formData
    else if (json !== undefined) init.body = JSON.stringify(json)

    response = await fetch(apiUrl(path, query), init)
  } catch (error) {
    // fetch 抛错有两种来源：网络层失败，或我们自己 abort（超时）。
    // 用 signal.aborted 区分，避免把超时误报成「后端不可达」。
    if (signal?.aborted) throw new ApiError({ code: 'ABORTED', message: '请求已取消。' })
    if (controller.signal.aborted) {
      throw new ApiError({ status: 0, code: 'TIMEOUT', message: '请求超时，请稍后重试。' })
    }
    throw toNetworkError(error)
  } finally {
    window.clearTimeout(timer)
    signal?.removeEventListener('abort', onExternalAbort)
  }

  if (!response.ok) {
    throw await normalizeHttpError(response)
  }

  // 204 或空 body（部分接口）不应触发 JSON.parse 错误
  const text = await response.text()
  if (!text) return undefined as T
  try {
    return JSON.parse(text) as T
  } catch {
    throw new ApiError({
      status: response.status,
      code: 'INVALID_RESPONSE',
      message: '后端返回的不是合法 JSON，接口契约可能已变更。',
    })
  }
}
