/**
 * 健康检查与管理接口。
 *
 * 契约依据：接口文档.md §4.1（health）、§4.7 - §4.10（文档入库 / 索引重建 / 任务进度 / 切片查询）。
 * 这一组接口对应 FR-08，依赖 RAG 链路，属于 P0/P1 的后半段。
 */

import { request } from '@/api/http'

// --------------------------------------------------------------------------- //
// 健康检查  GET /api/health
// --------------------------------------------------------------------------- //

/**
 * 单个依赖组件的健康状态。
 * 后端是 dict[str, Any]，各组件字段不统一（graph 带 nodes/checkpointer，
 * mcp 带 tools/degraded），因此这里保留宽松索引签名，渲染时按需取用。
 */
export interface ComponentHealth {
  ok: boolean
  detail?: string
  latency_ms?: number
  [key: string]: unknown
}

export interface HealthResponse {
  /** ok 全部正常 / degraded 部分降级但主链路可用 / down 主链路不可用 */
  status: 'ok' | 'degraded' | 'down'
  version: string
  components: Record<string, ComponentHealth>
  trace_id: string
}

/** 组件名 → 中文标题（接口文档 §4.1 的 components 键） */
export const COMPONENT_LABELS: Record<string, string> = {
  vector_store: '向量库',
  llm: '文本模型',
  vlm: '图片模型',
  mcp: 'MCP 工单检索',
  graph: 'LangGraph 图',
}

/** 健康检查（接口文档 §4.1）。供前端启动自检，也是 R7「MCP 加载失败」降级状态的暴露点。 */
export function health(verbose = false): Promise<HealthResponse> {
  return request<HealthResponse>('/health', { query: { verbose }, timeoutMs: 15_000 })
}

// --------------------------------------------------------------------------- //
// 文档入库  POST /api/admin/documents
// --------------------------------------------------------------------------- //

export interface DocumentUploadResponse {
  doc_id: number
  job_id: string
  status: string
  trace_id: string
}

/** 入库允许的文档类型（接口文档 §4.7） */
export const DOC_ACCEPT = '.pdf,.md,.txt,.docx,.pptx,.xlsx'

/** 文档分类（接口文档 §4.7 category 字段） */
export const DOC_CATEGORIES = ['设备维护', '工艺', '标准'] as const

/**
 * 上传文档并触发入库流水线（接口文档 §4.7）。
 *
 * **长耗时任务，异步执行**：立即返回 job_id（HTTP 202），
 * 用 getJob 轮询进度。入库链路：解析 → 抽图 → 清洗 → 切片 → 向量化 → 入库。
 */
export function uploadDocument(formData: FormData): Promise<DocumentUploadResponse> {
  return request<DocumentUploadResponse>('/admin/documents', {
    method: 'POST',
    formData,
    timeoutMs: 120_000,
  })
}

// --------------------------------------------------------------------------- //
// 索引重建  POST /api/admin/index/rebuild
// --------------------------------------------------------------------------- //

export interface IndexRebuildRequest {
  /** all 全量 / incremental 增量 */
  scope?: 'all' | 'incremental'
  /** 是否强制重建（忽略切片版本） */
  force?: boolean
}

export interface IndexRebuildResponse {
  job_id: string
  status: string
  trace_id: string
}

/** 一键重建索引（接口文档 §4.8，验收标准 ≤ 5 分钟）。 */
export function rebuildIndex(payload: IndexRebuildRequest = {}): Promise<IndexRebuildResponse> {
  return request<IndexRebuildResponse>('/admin/index/rebuild', { method: 'POST', json: payload })
}

// --------------------------------------------------------------------------- //
// 任务进度  GET /api/admin/jobs/{job_id}
// --------------------------------------------------------------------------- //

export interface JobStatusResponse {
  job_id: string
  status: 'pending' | 'running' | 'succeeded' | 'failed'
  /** 0–1 */
  progress: number
  /** 当前阶段描述，如「切片 128/540」 */
  message: string
  /** 完成后结果，如 {"chunks": 540, "images": 32} */
  result: Record<string, unknown> | null
  trace_id: string
}

/** 查询异步任务进度（接口文档 §4.9）。任务不存在时 404 JOB_NOT_FOUND。 */
export function getJob(jobId: string): Promise<JobStatusResponse> {
  return request<JobStatusResponse>(`/admin/jobs/${encodeURIComponent(jobId)}`)
}

// --------------------------------------------------------------------------- //
// 切片查询  GET /api/admin/chunks
// --------------------------------------------------------------------------- //

export interface ChunkItem {
  chunk_id: string
  doc: string | null
  version: string | null
  section: string | null
  page: number | null
  token_len: number | null
  text: string
  image_url: string | null
}

export interface ChunkListResponse {
  items: ChunkItem[]
  total: number
  limit: number
  offset: number
  trace_id: string
}

export interface ListChunksQuery {
  doc_id?: number
  q?: string
  page?: number
  limit?: number
  offset?: number
}

/** 切片查询，**调分块参数时用**（接口文档 §4.10）。 */
export function listChunks(query: ListChunksQuery = {}): Promise<ChunkListResponse> {
  return request<ChunkListResponse>('/admin/chunks', { query: { ...query } })
}
