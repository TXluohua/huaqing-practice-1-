/**
 * 备件商城与采购接口（功能②，接口文档 §10.2）。
 *
 * **独立模块**：只依赖 `@/api/http`，与问答（会话 / SSE / `qa_id`）无任何耦合。
 *
 * 三条业务边界（页面必须体现，不是可选项）：
 *   1. **不对供应商真实下单** —— 只生成内部采购申请单，「人工确认」= `approve`；
 *   2. 替代件必须有兼容性依据（无依据后端直接 400 `SUBSTITUTE_BASIS_REQUIRED`）；
 *   3. 价格与库存由后端回台账取，**前端不传价格、不自己算金额**（用返回的 `total_amount`）。
 *
 * 类型逐字段对齐 `backend/schemas.py`（PartItem / PartOrderResponse / SettlementResponse）。
 */

import { request } from '@/api/http'
import type { Citation } from '@/api/contracts'

// --------------------------------------------------------------------------- //
// 商城目录
// --------------------------------------------------------------------------- //

export interface SubstitutePart {
  part: string
  code: string
  stock: number
  unit: string
  lead_time_days: number
  /** 兼容性依据（台账原文）—— 展示时不要省略 */
  basis: string
  price_cny?: number | null
  supplier?: string | null
  requires_approval?: boolean
}

export interface PartItem {
  code: string
  part: string
  device_model: string
  spec: string
  stock: number
  unit: string
  location: string
  lead_time_days: number
  price_cny: number
  supplier: string
  /** `ok` / `low`（缺货判断用 `stock === 0`） */
  stock_status: string
  substitutes: SubstitutePart[]
  /** 禁止替代清单，已是成品句「禁止替代：X —— 原因」，红色警示展示 */
  forbidden: string[]
  /** 注意：`GET /parts/{code}` 是唯一不带 trace_id 的接口 */
  trace_id?: string
}

export interface PartCatalogResponse {
  items: PartItem[]
  total: number
  trace_id: string
}

export function listCatalog(query: {
  device_model?: string
  keyword?: string
  limit?: number
  offset?: number
}): Promise<PartCatalogResponse> {
  return request<PartCatalogResponse>('/parts/catalog', { query })
}

export function getPart(code: string): Promise<PartItem> {
  return request<PartItem>(`/parts/${encodeURIComponent(code)}`)
}

// --------------------------------------------------------------------------- //
// 采购申请单（购物车 = draft 状态的申请单）
// --------------------------------------------------------------------------- //

export type OrderStatus = 'draft' | 'submitted' | 'approved' | 'rejected' | 'received' | 'cancelled'

export interface OrderItem {
  code: string
  part: string
  qty: number
  unit: string
  unit_price: number
  amount: number
  is_substitute: boolean
  /** 替代件的兼容性依据（主件为空串） */
  basis: string
  stock: number
  lead_time_days: number
}

export interface PartOrder {
  id: number
  order_no: string
  status: OrderStatus | string
  device_model: string
  purpose: string
  applicant: string
  items: OrderItem[]
  total_amount: number
  currency: string
  note: string
  created_at: string | null
  updated_at: string | null
  /** 后端给出的可执行动作，**按钮必须由它驱动，不要前端硬编码状态机** */
  allowed_actions: string[]
  trace_id: string
}

export interface OrderListResponse {
  items: PartOrder[]
  total: number
  limit: number
  offset: number
  trace_id: string
}

export interface OrderItemRequest {
  code: string
  qty: number
  is_substitute?: boolean
  /** 只写进订单 note 的审计行 */
  operator?: string
}

export interface OrderCreateRequest {
  items: OrderItemRequest[]
  device_model?: string
  purpose?: string
  applicant?: string
  note?: string
}

export interface OrderActionRequest {
  operator?: string
  note?: string
  reason?: string
}

export interface Settlement {
  id: number
  order_id: number
  order_no: string
  amount: number
  currency: string
  method: string
  invoice_no: string
  operator: string
  note: string
  settled_at: string | null
  trace_id: string
}

export interface SettlementListResponse {
  items: Settlement[]
  total: number
  /** 过滤后全量金额合计（不受分页影响），页面直接用它，不要前端累加 */
  total_amount: number
  trace_id: string
}

export interface SettlementRequest {
  amount?: number | null
  method?: string
  invoice_no?: string
  operator?: string
  note?: string
}

export function listOrders(query: {
  status?: string
  applicant?: string
  limit?: number
  offset?: number
}): Promise<OrderListResponse> {
  return request<OrderListResponse>('/parts/orders', { query })
}

export function getOrder(orderId: number): Promise<PartOrder> {
  return request<PartOrder>(`/parts/orders/${orderId}`)
}

export function createOrder(payload: OrderCreateRequest): Promise<PartOrder> {
  return request<PartOrder>('/parts/orders', { method: 'POST', json: payload })
}

/** 加入购物车（同编码累加数量，不产生重复行） */
export function addCartItem(orderId: number, payload: OrderItemRequest): Promise<PartOrder> {
  return request<PartOrder>(`/parts/orders/${orderId}/items`, { method: 'POST', json: payload })
}

/** 改数量（1~999） */
export function updateCartItem(
  orderId: number,
  code: string,
  payload: { qty: number; operator?: string },
): Promise<PartOrder> {
  return request<PartOrder>(`/parts/orders/${orderId}/items/${encodeURIComponent(code)}`, {
    method: 'PATCH',
    json: payload,
  })
}

/** 移出购物车（允许清空；空车不能提交） */
export function removeCartItem(
  orderId: number,
  code: string,
  payload: { operator?: string } = {},
): Promise<PartOrder> {
  return request<PartOrder>(`/parts/orders/${orderId}/items/${encodeURIComponent(code)}`, {
    method: 'DELETE',
    json: payload,
  })
}

export function submitOrder(orderId: number, payload: OrderActionRequest = {}): Promise<PartOrder> {
  return request<PartOrder>(`/parts/orders/${orderId}/submit`, { method: 'POST', json: payload })
}

/** 人工确认（approve）—— 系统的唯一确认点，之后才能收货与结算 */
export function approveOrder(orderId: number, payload: OrderActionRequest = {}): Promise<PartOrder> {
  return request<PartOrder>(`/parts/orders/${orderId}/approve`, { method: 'POST', json: payload })
}

/** 驳回；`reason` 以 `cancel: ` 开头时后端按「取消」处理 */
export function rejectOrder(orderId: number, payload: OrderActionRequest): Promise<PartOrder> {
  return request<PartOrder>(`/parts/orders/${orderId}/reject`, { method: 'POST', json: payload })
}

export function receiveOrder(orderId: number, payload: OrderActionRequest = {}): Promise<PartOrder> {
  return request<PartOrder>(`/parts/orders/${orderId}/receive`, { method: 'POST', json: payload })
}

/** 登记结算（幂等：重复结算 409 ALREADY_SETTLED） */
export function settleOrder(orderId: number, payload: SettlementRequest): Promise<Settlement> {
  return request<Settlement>(`/parts/orders/${orderId}/settle`, { method: 'POST', json: payload })
}

export function listSettlements(query: {
  order_no?: string
  limit?: number
  offset?: number
}): Promise<SettlementListResponse> {
  return request<SettlementListResponse>('/parts/settlements', { query })
}

// --------------------------------------------------------------------------- //
// 展示辅助
// --------------------------------------------------------------------------- //

/** `allowed_actions` → 按钮文案（后端是唯一事实来源） */
export const ACTION_LABELS: Record<string, string> = {
  submit: '提交申请',
  approve: '人工确认',
  reject: '驳回',
  cancel: '撤销申请',
  receive: '确认收货',
  settle: '登记结算',
}

export const ORDER_STATUS_META: Record<string, { label: string; type: 'primary' | 'success' | 'warning' | 'danger' | 'info' }> = {
  draft: { label: '草稿（购物车）', type: 'info' },
  submitted: { label: '已提交（待确认）', type: 'warning' },
  approved: { label: '已确认（待收货）', type: 'primary' },
  received: { label: '已收货（待结算）', type: 'primary' },
  rejected: { label: '已驳回', type: 'danger' },
  cancelled: { label: '已撤销', type: 'info' },
}

/** 库存状态文案：`low` + `stock === 0` 才是真缺货 */
export function stockText(part: PartItem): string {
  if (part.stock <= 0) return `缺货（${part.lead_time_days} 天到货）`
  if (part.stock_status === 'low') return `库存偏低（${part.stock} ${part.unit}）`
  return `${part.stock} ${part.unit}`
}

/** 引用（依据）摘要文案 */
export function evidenceText(citation: Citation | undefined): string {
  if (!citation) return '无依据'
  const parts = [citation.doc ?? '未知文档']
  if (citation.version) parts.push(citation.version)
  if (citation.section) parts.push(citation.section)
  return parts.join(' · ')
}
