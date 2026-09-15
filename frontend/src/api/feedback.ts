/**
 * 反馈接口。
 *
 * 契约依据：接口文档.md §4.6（FR-07 四态反馈）。
 * 驳回案例由后端 service 记入 data/eval/badcases.md。
 */

import { request } from '@/api/http'

/** 四态反馈：采纳 / 部分采纳 / 驳回 / 修正 */
export type FeedbackType = 'adopt' | 'partial' | 'reject' | 'correct'

export interface FeedbackRequest {
  qa_id: number
  type: FeedbackType
  /** 备注，≤500 字 */
  comment?: string | null
  /** type=correct 时必填，否则后端返回 400 CORRECTED_ANSWER_REQUIRED */
  corrected_answer?: string | null
}

export interface FeedbackResponse {
  feedback_id: number
  recorded: boolean
  created_at: string
  trace_id: string
}

/** 四态反馈的中文标签与配色，供 FeedbackBar 渲染。 */
export const FEEDBACK_LABELS: Record<FeedbackType, string> = {
  adopt: '采纳',
  partial: '部分采纳',
  reject: '驳回',
  correct: '修正',
}

/** 提交反馈（接口文档 §4.6）。qa_id 不存在时后端返回 404 QA_NOT_FOUND。 */
export function submitFeedback(payload: FeedbackRequest): Promise<FeedbackResponse> {
  return request<FeedbackResponse>('/feedback', { method: 'POST', json: payload })
}
