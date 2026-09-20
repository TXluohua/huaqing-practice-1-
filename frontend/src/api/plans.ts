/**
 * 维护计划接口（功能①，接口文档 §10.1）。
 *
 * **独立模块**：本文件只依赖 `@/api/http`，与问答（`@/api/chat`、SSE、会话）
 * 没有任何耦合 —— 维护计划页不需要会话、`qa_id` 或流式事件即可工作。
 *
 * 类型逐字段对齐 `backend/schemas.py`（PlanItem / PlanGenerateRequest / PlanUpdateRequest），
 * 后端改字段时这里必须同步。
 */

import { request } from '@/api/http'
import type { Citation } from '@/api/contracts'

/** 计量口径：运行小时 / 生产片数 / 自然日 */
export type CycleBasis = 'hours' | 'wafers' | 'days'

/** 计划项状态（后端 PLAN_STATUSES） */
export type PlanStatus = 'planned' | 'due' | 'done' | 'skipped'

/** 一条维护计划项（依据必须可溯源：`evidence` 非空） */
export interface PlanItem {
  id: number | null
  device_model: string
  device_code: string
  item_name: string
  item_type: string
  cycle_basis: CycleBasis | string
  cycle_value: number
  baseline_value: number
  current_value: number
  /** 距离到期还差多少计量单位（负数 = 已超期） */
  remaining: number
  /** 到期时间；`wafers` 口径（估算不出产线速率）与按需执行时为 null */
  due_at: string | null
  status: PlanStatus | string
  evidence: Citation[]
  chunk_id: string
  note: string
  created_at?: string | null
  updated_at?: string | null
  completed_at?: string | null
}

export interface PlanGenerateRequest {
  device_model: string
  device_code?: string
  runtime_hours?: number
  wafer_count?: number
  hours_since_pm?: number | null
  wafers_since_pm?: number | null
  top_k?: number
  /** false = 只预览不落库 */
  persist?: boolean
}

export interface PlanGenerateResponse {
  device_model: string
  device_code: string
  items: PlanItem[]
  /** 知识库里查不到周期的维护项（格式「名称：原因」），**必须展示，不要隐藏** */
  uncovered: string[]
  trace_id: string
}

export interface PlanListResponse {
  items: PlanItem[]
  total: number
  limit: number
  offset: number
  trace_id: string
}

export interface PlanUpdateRequest {
  note?: string | null
  /** 完成时回填的计量值（滚动到下一个周期） */
  completed_value?: number | null
}

/** 生成维护计划（`persist=false` 时只预览不落库） */
export function generatePlan(payload: PlanGenerateRequest): Promise<PlanGenerateResponse> {
  return request<PlanGenerateResponse>('/plans/generate', { method: 'POST', json: payload })
}

/** 计划列表（后端按 `remaining` 升序，最紧急在前） */
export function listPlans(query: {
  device_model?: string
  status?: string
  limit?: number
  offset?: number
}): Promise<PlanListResponse> {
  return request<PlanListResponse>('/plans', { query })
}

/** 标记完成（自动滚动到下一周期） */
export function completePlan(planId: number, payload: PlanUpdateRequest): Promise<PlanItem> {
  return request<PlanItem>(`/plans/${planId}/complete`, { method: 'POST', json: payload })
}

/** 跳过（`note` 建议填原因） */
export function skipPlan(planId: number, payload: PlanUpdateRequest): Promise<PlanItem> {
  return request<PlanItem>(`/plans/${planId}/skip`, { method: 'POST', json: payload })
}

/** 口径中文名（展示用） */
export const BASIS_LABELS: Record<string, string> = {
  hours: '运行小时',
  wafers: '生产片数',
  days: '自然日',
}

/** 剩余量的单位（与 BASIS_LABELS 配套） */
export const BASIS_UNITS: Record<string, string> = {
  hours: '小时',
  wafers: '片',
  days: '天',
}

/** 状态中文名 + 语义色（Element Plus tag type） */
export const STATUS_META: Record<string, { label: string; type: 'primary' | 'danger' | 'success' | 'info' }> = {
  planned: { label: '未到期', type: 'primary' },
  due: { label: '已到期', type: 'danger' },
  done: { label: '已完成', type: 'success' },
  skipped: { label: '已跳过', type: 'info' },
}

/** 剩余量的可读文案：负数说「已超期」，不再显示负号 */
export function remainingText(item: PlanItem): string {
  const unit = BASIS_UNITS[item.cycle_basis] ?? ''
  if (item.remaining <= 0) return `已超期 ${Math.abs(item.remaining)}${unit}`
  return `还剩 ${item.remaining}${unit}`
}
