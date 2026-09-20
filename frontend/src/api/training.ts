/**
 * 考核认证接口（功能③，接口文档 §10.3）。
 *
 * **独立模块**：只依赖 `@/api/http`，与问答（会话 / SSE）无任何耦合 ——
 * 考核页自己出题、自己交卷，不经过聊天页。注意与问答页的「培训模式」
 * （`mode=training` 的分层讲解）是两件事：那是问答能力，这里是独立考核功能。
 *
 * 两条硬边界（页面必须体现）：
 *   1. 每题都带 `evidence`（依据），没有依据的题后端不会出（一题都出不来直接 422）；
 *   2. **判分不自动发证** —— 交卷响应的 `certification_id` 恒为 null，
 *      发证必须用户显式点击并指定等级与有效期，证书口径是「内部授权」。
 */

import { request } from '@/api/http'
import type { Citation } from '@/api/contracts'

// --------------------------------------------------------------------------- //
// 出题 / 试卷
// --------------------------------------------------------------------------- //

/** 题型：单选 / 多选 / 判断 / 简答 */
export type QuestionType = 'single' | 'multiple' | 'judgement' | 'short'

export interface QuizItem {
  no: number
  question: string
  type: QuestionType | string
  options: string[]
  /** 单选=下标、多选=下标数组、判断=bool、简答=要点数组 */
  answer: unknown
  explanation: string
  /** 出题依据（必须非空） */
  evidence: Citation[]
}

export interface Quiz {
  id: number
  device_model: string
  topic: string
  level: string
  /** **实际**题量；依据校验剔除不合规题目后可能少于请求量 */
  n_items: number
  /** 请求的题量；回看试卷（GET）时为 null */
  requested_items?: number | null
  /** 因依据校验被剔除的题量；回看试卷（GET）时为 null */
  dropped_items?: number | null
  items: QuizItem[]
  /** `llm:<model>` 或 `extractive-fallback`（未配文本模型时的确定性降级） */
  generator: string
  created_at: string | null
  trace_id: string
}

export interface QuizGenerateRequest {
  device_model?: string
  topic?: string
  level?: string
  n_items?: number
  question_types?: QuestionType[]
}

export interface QuizResultItem {
  no: number
  question: string
  correct: boolean
  expected: unknown
  got: unknown
  explanation: string
  evidence: Citation[]
}

export interface QuizSubmitResponse {
  attempt_id: number
  quiz_id: number
  trainee: string
  score: number
  passed: boolean
  pass_line: number
  detail: QuizResultItem[]
  /** 恒为 null：后端不自动发证 */
  certification_id: number | null
  trace_id: string
}

export function generateQuiz(payload: QuizGenerateRequest): Promise<Quiz> {
  return request<Quiz>('/training/quizzes', { method: 'POST', json: payload })
}

export function getQuiz(quizId: number): Promise<Quiz> {
  return request<Quiz>(`/training/quizzes/${quizId}`)
}

export function submitQuiz(
  quizId: number,
  payload: { trainee: string; answers: unknown[]; duration_s?: number },
): Promise<QuizSubmitResponse> {
  return request<QuizSubmitResponse>(`/training/quizzes/${quizId}/submit`, {
    method: 'POST',
    json: payload,
  })
}

// --------------------------------------------------------------------------- //
// 认证
// --------------------------------------------------------------------------- //

export interface Certification {
  id: number
  trainee: string
  device_model: string
  level: string
  attempt_id: number
  quiz_id: number
  score: number
  /** 默认「内部授权」，不代表原厂认证 */
  issuer: string
  issued_at: string | null
  expires_at: string | null
  status: string
  /** 距到期天数（负数 = 已过期） */
  days_to_expiry: number | null
  note: string
  trace_id: string
}

export interface CertificationListResponse {
  items: Certification[]
  total: number
  trace_id: string
}

export interface CertificationRequest {
  trainee: string
  device_model: string
  level: string
  /** 必须是一次**通过**的考核 */
  attempt_id: number
  issuer?: string
  valid_days?: number
  note?: string
}

export function listCertifications(query: {
  trainee?: string
  device_model?: string
  status?: string
  limit?: number
  offset?: number
}): Promise<CertificationListResponse> {
  return request<CertificationListResponse>('/training/certifications', { query })
}

/** 到期提醒（含已过期），用于看板红点 / 提醒卡片 */
export function listExpiringCertifications(days = 30): Promise<CertificationListResponse> {
  return request<CertificationListResponse>('/training/certifications/expiring', { query: { days } })
}

/** 发证：必须基于一次通过的考核，否则 400 ATTEMPT_NOT_PASSED */
export function issueCertification(payload: CertificationRequest): Promise<Certification> {
  return request<Certification>('/training/certifications', { method: 'POST', json: payload })
}

// --------------------------------------------------------------------------- //
// 展示辅助
// --------------------------------------------------------------------------- //

export const TYPE_LABELS: Record<string, string> = {
  single: '单选',
  multiple: '多选',
  judgement: '判断',
  short: '简答',
}

export const LEVEL_LABELS: Record<string, string> = {
  basic: '基础',
  advanced: '进阶',
}

export const CERT_LEVELS = ['L1', 'L2', 'L3'] as const

/** 认证状态（后端给 status，这里给中文与语义色；到期天数由 days_to_expiry 判定） */
export function certStatusMeta(cert: Certification): {
  label: string
  type: 'success' | 'warning' | 'danger' | 'info'
} {
  if (cert.status === 'revoked') return { label: '已吊销', type: 'info' }
  const days = cert.days_to_expiry
  if (days !== null && days < 0) return { label: `已过期 ${Math.abs(days)} 天`, type: 'danger' }
  if (days !== null && days <= 30) return { label: `${days} 天后到期`, type: 'warning' }
  return { label: '有效', type: 'success' }
}

/**
 * 答题输入 → 提交格式（接口文档 §10.3）。
 * 页面内部统一用字符串下标存答案，提交前按题型转换。
 */
export function toSubmitAnswer(type: string, draft: unknown): unknown {
  if (type === 'multiple') {
    const list = Array.isArray(draft) ? draft : []
    return list.map((value) => Number(value)).filter((value) => Number.isInteger(value))
  }
  if (type === 'single') {
    if (draft === undefined || draft === null || draft === '') return null
    return Number(draft)
  }
  if (type === 'judgement') {
    return draft === true || draft === 'true'
  }
  return typeof draft === 'string' ? draft : ''
}
