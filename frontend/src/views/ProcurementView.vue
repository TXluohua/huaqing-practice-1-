<script setup lang="ts">
/**
 * 采购结算页（业务组页面，**与问答链路完全独立** —— 不用会话 / SSE / qa_id）。
 *
 * 两个 Tab：
 *   1. 申请单：采购申请单列表（状态 / 申请人筛选 + 分页）+ 详情抽屉（明细、审计 note、动作按钮）；
 *   2. 结算台账：`order_no` 模糊搜索 + 分页，顶部卡片直接用后端汇总的 `total_amount`。
 *
 * 关键约定（照着后端契约走，不在前端重实现）：
 *   * 动作按钮**完全由响应里的 `allowed_actions` 驱动**，文案取 `ACTION_LABELS`；
 *     前端**不硬编码状态机** —— 后端把 `draft/submitted/...` 的合法迁移固化在 `allowed_actions` 里，
 *     前端只负责渲染。`approve` 就是「人工确认」，是系统唯一的确认点。
 *   * `reject` 与 `cancel` 复用同一个后端端点（`POST …/reject`），靠 `reason` 前缀区分：
 *     撤销必须以 `cancel: ` 开头（如 `cancel: 计划变更`），所以界面上给两个独立输入框与两个按钮，
 *     并把两者的语义差别写清楚，避免「想撤销却点了驳回」。
 *   * 任何动作拿到 409 `INVALID_STATE` → 自动重取该订单详情 + 提示「状态已变更，请重试」。
 *   * 结算**只登记台账，不做财务过账**；同一订单重复结算后端返回 409 `ALREADY_SETTLED`。
 */
import { Refresh, Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { formatTime, humanizeError, isApiError, type ApiError } from '@/api/http'
import {
  ACTION_LABELS,
  ORDER_STATUS_META,
  approveOrder,
  getOrder,
  listOrders,
  listSettlements,
  receiveOrder,
  rejectOrder,
  settleOrder,
  submitOrder,
  type PartOrder,
  type Settlement,
} from '@/api/parts'

const router = useRouter()

/** 状态筛选：`''` = 全部；文案直接取 `ORDER_STATUS_META`，避免两处文案漂移 */
const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: '', label: '全部' },
  ...(['draft', 'submitted', 'approved', 'received', 'rejected', 'cancelled'] as const).map(
    (key) => ({ value: key, label: ORDER_STATUS_META[key].label }),
  ),
]

function statusMeta(status: string): { label: string; type: 'primary' | 'success' | 'warning' | 'danger' | 'info' } {
  return ORDER_STATUS_META[status] ?? { label: status || '未知', type: 'info' }
}

/** 金额文案：`0` 表示明细单价在台账里缺失（不是「免费」），单独给出文案而不是显示 0 元 */
function amountText(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  if (value === 0) return '台账未提供单价'
  return `¥ ${value.toFixed(2)}`
}

// ------------------------------------------------------------------ 申请单

const activeTab = ref<'orders' | 'settlements'>('orders')

const orderQuery = reactive({ status: '', applicant: '' })
const orders = ref<PartOrder[]>([])
const orderTotal = ref(0)
const orderLimit = ref(20)
const orderOffset = ref(0)
const orderLoading = ref(false)
const orderError = ref<ApiError | null>(null)

async function loadOrders(): Promise<void> {
  orderLoading.value = true
  orderError.value = null
  try {
    const response = await listOrders({
      status: orderQuery.status || undefined,
      applicant: orderQuery.applicant.trim() || undefined,
      limit: orderLimit.value,
      offset: orderOffset.value,
    })
    orders.value = response.items
    orderTotal.value = response.total
  } catch (error) {
    orders.value = []
    orderTotal.value = 0
    orderError.value = isApiError(error) ? error : null
  } finally {
    orderLoading.value = false
  }
}

function onOrderSearch(): void {
  orderOffset.value = 0
  void loadOrders()
}

function onOrderReset(): void {
  orderQuery.status = ''
  orderQuery.applicant = ''
  orderOffset.value = 0
  void loadOrders()
}

function onOrderPage(target: number): void {
  orderOffset.value = (target - 1) * orderLimit.value
  void loadOrders()
}

// ------------------------------------------------------------ 详情与动作

const drawerVisible = ref(false)
const detail = ref<PartOrder | null>(null)
const detailLoading = ref(false)
const detailError = ref<ApiError | null>(null)
const detailId = ref<number | null>(null)
const actionBusy = ref(false)

/** 操作人（选填）：会写进订单 note 的审计行 */
const operator = ref('')
const rejectReason = ref('')
const cancelReason = ref('')

/** 明细里的审计行：`note` 由后端按行追加，这里逐行显示 */
const noteLines = computed(() =>
  (detail.value?.note ?? '')
    .split('\n')
    .map((line) => line.trim())
    .filter((line) => line.length > 0),
)

/** 动作按钮的唯一来源：后端响应里的 `allowed_actions` */
const allowedActions = computed(() => detail.value?.allowed_actions ?? [])

/** 直接点击即执行的动作（reject / cancel 需要先填原因，单独成块） */
const directActions = computed(() =>
  allowedActions.value.filter((action) => action !== 'reject' && action !== 'cancel'),
)
const canReject = computed(() => allowedActions.value.includes('reject'))
const canCancel = computed(() => allowedActions.value.includes('cancel'))

function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? action
}

async function openDetail(orderId: number): Promise<void> {
  drawerVisible.value = true
  detailId.value = orderId
  detail.value = null
  detailError.value = null
  rejectReason.value = ''
  cancelReason.value = ''
  await refreshDetail()
}

/** 拉取（或重新拉取）详情：409 冲突后也走这里，保证界面与后端状态一致 */
async function refreshDetail(): Promise<void> {
  const orderId = detailId.value
  if (orderId === null) return

  detailLoading.value = true
  detailError.value = null
  try {
    const response = await getOrder(orderId)
    detail.value = response
    if (!operator.value) operator.value = response.applicant
  } catch (error) {
    detail.value = null
    detailError.value = isApiError(error) ? error : null
  } finally {
    detailLoading.value = false
  }
}

function retryDetail(): void {
  void refreshDetail()
}

/**
 * 动作统一收口：成功就覆盖详情与列表，409 `INVALID_STATE` 则自动重取详情。
 * 冲突处理是硬要求 —— 别人先改了状态时，界面上的按钮已经过期，必须刷新后再提示重试。
 */
async function handleActionError(error: unknown, action: string): Promise<void> {
  const code = isApiError(error) ? error.code : ''
  if (code === 'INVALID_STATE') {
    ElMessage.warning('状态已变更，请重试')
    await refreshDetail()
    await loadOrders()
    return
  }
  ElMessage.error(`${actionLabel(action)}失败：${humanizeError(error)}`)
}

async function runOrderAction(action: string, execute: () => Promise<PartOrder>): Promise<void> {
  actionBusy.value = true
  try {
    const response = await execute()
    detail.value = response
    ElMessage.success(`${actionLabel(action)}成功：${response.order_no}`)
    // 列表里的状态列 / 合计是动作的产物，同步刷新
    await loadOrders()
  } catch (error) {
    await handleActionError(error, action)
  } finally {
    actionBusy.value = false
  }
}

/** 直接动作：submit / approve / receive 走各自的端点；settle 需要额外表单，开对话框 */
async function onDirectAction(action: string): Promise<void> {
  const order = detail.value
  if (!order) return

  if (action === 'settle') {
    openSettle()
    return
  }

  const payload = { operator: operator.value.trim() || undefined }
  if (action === 'submit') await runOrderAction(action, () => submitOrder(order.id, payload))
  else if (action === 'approve') await runOrderAction(action, () => approveOrder(order.id, payload))
  else if (action === 'receive') await runOrderAction(action, () => receiveOrder(order.id, payload))
  else ElMessage.warning(`后端返回了未识别的动作：${action}`)
}

/** 驳回：由审批人给出反对意见，进入「已驳回」 */
async function onReject(): Promise<void> {
  const order = detail.value
  if (!order) return
  const reason = rejectReason.value.trim()
  if (!reason) {
    ElMessage.warning('驳回必须填写原因（会写入订单审计行）。')
    return
  }

  await runOrderAction('reject', () =>
    rejectOrder(order.id, { reason, operator: operator.value.trim() || undefined }),
  )
}

/**
 * 撤销：把草稿 / 已提交的申请作废。后端复用 reject 端点，
 * `reason` 必须带 `cancel: ` 前缀，否则会被当成驳回（两者落到的状态不同）。
 */
async function onCancel(): Promise<void> {
  const order = detail.value
  if (!order) return
  const reason = cancelReason.value.trim()
  if (!reason) {
    ElMessage.warning('撤销必须填写原因（例如「计划变更」），会写成 `cancel: 原因` 提交。')
    return
  }

  // 用户自己写了前缀就不再重复拼接
  const body = reason.replace(/^(cancel|cancel\s*:|取消|取消\s*[:：])\s*/i, '').trim()
  await runOrderAction('cancel', () =>
    rejectOrder(order.id, {
      reason: `cancel: ${body || '计划变更'}`,
      operator: operator.value.trim() || undefined,
    }),
  )
}

// ------------------------------------------------------------------ 结算登记

const settleVisible = ref(false)
const settleSubmitting = ref(false)
const settleForm = reactive<{
  amount: number | null
  method: string
  invoiceNo: string
  operator: string
}>({ amount: null, method: '月结', invoiceNo: '', operator: '' })

function openSettle(): void {
  const order = detail.value
  if (!order) return
  // 金额默认留空 = 用订单金额（后端行为），不做前端默认填充
  settleForm.amount = null
  settleForm.method = '月结'
  settleForm.invoiceNo = ''
  settleForm.operator = operator.value.trim()
  settleVisible.value = true
}

async function submitSettle(): Promise<void> {
  const order = detail.value
  if (!order) return

  settleSubmitting.value = true
  try {
    const settlement = await settleOrder(order.id, {
      amount: settleForm.amount ?? undefined,
      method: settleForm.method.trim() || undefined,
      invoice_no: settleForm.invoiceNo.trim() || undefined,
      operator: settleForm.operator.trim() || undefined,
    })
    settleVisible.value = false
    ElMessage.success(`已登记结算 ${settlement.order_no}：${amountText(settlement.amount)}（只登记台账，财务以 ERP 为准）`)
    await refreshDetail()
    await loadOrders()
    await loadSettlements()
  } catch (error) {
    await handleActionError(error, 'settle')
  } finally {
    settleSubmitting.value = false
  }
}

// ---------------------------------------------------------------- 结算台账

const settleQuery = reactive({ orderNo: '' })
const settlements = ref<Settlement[]>([])
const settleTotal = ref(0)
/** 后端汇总的过滤后全量金额（不受分页影响），页面直接用它，**不前端累加** */
const settleAmount = ref(0)
const settleLimit = ref(20)
const settleOffset = ref(0)
const settleLoading = ref(false)
const settleError = ref<ApiError | null>(null)

const settleCurrency = computed(() => settlements.value[0]?.currency || 'CNY')

async function loadSettlements(): Promise<void> {
  settleLoading.value = true
  settleError.value = null
  try {
    const response = await listSettlements({
      order_no: settleQuery.orderNo.trim() || undefined,
      limit: settleLimit.value,
      offset: settleOffset.value,
    })
    settlements.value = response.items
    settleTotal.value = response.total
    settleAmount.value = response.total_amount
  } catch (error) {
    settlements.value = []
    settleTotal.value = 0
    settleAmount.value = 0
    settleError.value = isApiError(error) ? error : null
  } finally {
    settleLoading.value = false
  }
}

function onSettleSearch(): void {
  settleOffset.value = 0
  void loadSettlements()
}

function onSettleReset(): void {
  settleQuery.orderNo = ''
  settleOffset.value = 0
  void loadSettlements()
}

function onSettlePage(target: number): void {
  settleOffset.value = (target - 1) * settleLimit.value
  void loadSettlements()
}

function goParts(): void {
  void router.push('/parts')
}

onMounted(() => {
  void loadOrders()
  void loadSettlements()
})
</script>

<template>
  <div class="proc">
    <div class="proc__inner">
      <header class="proc__head">
        <div class="proc__head-text">
          <h1 class="proc__title">采购结算</h1>
          <p class="proc__desc">
            采购申请单的流转与结算台账。提交只是拟一张内部<strong>采购申请单，不对供应商直接下单</strong>；
            「人工确认」（approve）是系统<strong>唯一的确认点</strong>，确认后才可收货与结算。
            结算<strong>只登记台账，不做财务过账</strong>。
          </p>
        </div>
        <el-button size="small" @click="goParts">去备件商城选件</el-button>
      </header>

      <el-tabs v-model="activeTab" class="proc__tabs">
        <!-- ------------------------------------------------------ 申请单 -->
        <el-tab-pane label="申请单" name="orders">
          <section class="panel">
            <div class="panel__head">
              <div class="panel__head-text">
                <h2 class="panel__title">采购申请单</h2>
                <p class="panel__desc">
                  按钮完全由后端返回的 allowed_actions 驱动，前端不判断状态机；状态冲突会自动刷新后提示重试。
                </p>
              </div>
              <span v-if="orderTotal" class="count tnum">{{ orderTotal }} 张</span>
            </div>

            <div class="panel__body">
              <div class="row">
                <el-select v-model="orderQuery.status" size="small" class="row__field--sm">
                  <el-option
                    v-for="item in STATUS_FILTERS"
                    :key="item.value"
                    :label="item.label"
                    :value="item.value"
                  />
                </el-select>
                <el-input
                  v-model="orderQuery.applicant"
                  size="small"
                  class="row__field"
                  placeholder="申请人（精确匹配）"
                  clearable
                  @keyup.enter="onOrderSearch"
                />
                <el-button size="small" type="primary" :icon="Search" @click="onOrderSearch">
                  搜索
                </el-button>
                <!-- 刷新保持当前页与筛选条件，只重取数据 -->
                <el-button size="small" :icon="Refresh" @click="loadOrders">刷新</el-button>
                <el-button size="small" @click="onOrderReset">重置</el-button>
              </div>

              <div v-if="orderError" class="note note--error">
                <p class="note__title">申请单列表加载失败：{{ orderError.code }}</p>
                <p class="note__text">{{ humanizeError(orderError) }}</p>
                <el-button size="small" class="note__action" @click="loadOrders">重试</el-button>
              </div>

              <el-table v-else v-loading="orderLoading" :data="orders" size="small">
                <el-table-column prop="order_no" label="单号" width="180">
                  <template #default="{ row }">
                    <span class="text-mono">{{ row.order_no }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="状态" width="130">
                  <template #default="{ row }">
                    <el-tag size="small" :type="statusMeta(row.status).type" effect="plain">
                      {{ statusMeta(row.status).label }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column prop="applicant" label="申请人" width="110" show-overflow-tooltip>
                  <template #default="{ row }">{{ row.applicant || '—' }}</template>
                </el-table-column>
                <el-table-column prop="device_model" label="设备型号" width="120" show-overflow-tooltip>
                  <template #default="{ row }">{{ row.device_model || '—' }}</template>
                </el-table-column>
                <el-table-column prop="purpose" label="用途" min-width="170" show-overflow-tooltip>
                  <template #default="{ row }">{{ row.purpose || '—' }}</template>
                </el-table-column>
                <el-table-column label="明细" width="76">
                  <template #default="{ row }">
                    <span class="tnum">{{ row.items.length }} 条</span>
                  </template>
                </el-table-column>
                <el-table-column label="合计金额" width="130">
                  <template #default="{ row }">
                    <span class="tnum">{{ amountText(row.total_amount) }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="创建时间" width="160">
                  <template #default="{ row }">
                    <span class="tnum">{{ formatTime(row.created_at) }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="操作" width="86" fixed="right">
                  <template #default="{ row }">
                    <el-button size="small" link type="primary" @click="openDetail(row.id)">
                      详情
                    </el-button>
                  </template>
                </el-table-column>
                <template #empty>
                  <div class="empty">
                    <p>没有匹配的申请单。</p>
                    <el-button size="small" @click="goParts">
                      去备件商城选件，加入购物车后提交申请
                    </el-button>
                  </div>
                </template>
              </el-table>

              <el-pagination
                v-if="orderTotal > orderLimit"
                class="pager"
                layout="prev, pager, next, total"
                size="small"
                :current-page="Math.floor(orderOffset / orderLimit) + 1"
                :page-size="orderLimit"
                :total="orderTotal"
                @current-change="onOrderPage"
              />
            </div>
          </section>
        </el-tab-pane>

        <!-- ---------------------------------------------------- 结算台账 -->
        <el-tab-pane label="结算台账" name="settlements">
          <section class="panel tally">
            <div class="tally__head">
              <div>
                <div class="tally__label">结算总额（后端汇总，不受分页影响）</div>
                <div class="tally__value tnum">
                  {{ amountText(settleAmount) }}
                  <span class="tally__currency">{{ settleCurrency }}</span>
                </div>
              </div>
              <div class="tally__side">
                <span class="count tnum">{{ settleTotal }} 笔</span>
                <p class="hint">只登记台账，不做财务过账：金额以 ERP 财务系统为准。</p>
              </div>
            </div>
          </section>

          <section class="panel">
            <div class="panel__head">
              <div class="panel__head-text">
                <h2 class="panel__title">结算台账</h2>
                <p class="panel__desc">按订单号模糊搜索；同一订单重复结算会被后端拒绝（ALREADY_SETTLED）。</p>
              </div>
            </div>

            <div class="panel__body">
              <div class="row">
                <el-input
                  v-model="settleQuery.orderNo"
                  size="small"
                  class="row__field"
                  placeholder="订单号模糊搜索，如 PO-2026"
                  clearable
                  @keyup.enter="onSettleSearch"
                />
                <el-button size="small" type="primary" :icon="Search" @click="onSettleSearch">
                  搜索
                </el-button>
                <el-button size="small" :icon="Refresh" @click="loadSettlements">刷新</el-button>
                <el-button size="small" @click="onSettleReset">重置</el-button>
              </div>

              <div v-if="settleError" class="note note--error">
                <p class="note__title">结算台账加载失败：{{ settleError.code }}</p>
                <p class="note__text">{{ humanizeError(settleError) }}</p>
                <el-button size="small" class="note__action" @click="loadSettlements">重试</el-button>
              </div>

              <el-table v-else v-loading="settleLoading" :data="settlements" size="small">
                <el-table-column label="结算单号" width="110">
                  <template #default="{ row }">
                    <span class="text-mono">#{{ row.id }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="order_no" label="订单号" width="180">
                  <template #default="{ row }">
                    <span class="text-mono">{{ row.order_no }}</span>
                  </template>
                </el-table-column>
                <el-table-column label="金额" width="130">
                  <template #default="{ row }">
                    <span class="tnum">{{ amountText(row.amount) }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="currency" label="币种" width="80" />
                <el-table-column prop="method" label="结算方式" width="110" show-overflow-tooltip>
                  <template #default="{ row }">{{ row.method || '—' }}</template>
                </el-table-column>
                <el-table-column prop="invoice_no" label="发票号" width="170" show-overflow-tooltip>
                  <template #default="{ row }">{{ row.invoice_no || '—' }}</template>
                </el-table-column>
                <el-table-column prop="operator" label="经办人" width="110" show-overflow-tooltip>
                  <template #default="{ row }">{{ row.operator || '—' }}</template>
                </el-table-column>
                <el-table-column label="结算时间" width="160">
                  <template #default="{ row }">
                    <span class="tnum">{{ formatTime(row.settled_at) }}</span>
                  </template>
                </el-table-column>
                <el-table-column prop="note" label="备注" min-width="160" show-overflow-tooltip>
                  <template #default="{ row }">{{ row.note || '—' }}</template>
                </el-table-column>
                <template #empty>没有匹配的结算记录</template>
              </el-table>

              <el-pagination
                v-if="settleTotal > settleLimit"
                class="pager"
                layout="prev, pager, next, total"
                size="small"
                :current-page="Math.floor(settleOffset / settleLimit) + 1"
                :page-size="settleLimit"
                :total="settleTotal"
                @current-change="onSettlePage"
              />
            </div>
          </section>
        </el-tab-pane>
      </el-tabs>
    </div>

    <!-- ------------------------------------------------------------ 详情抽屉 -->
    <el-drawer
      v-model="drawerVisible"
      size="680px"
      :title="detail ? `采购申请单 ${detail.order_no}` : '申请单详情'"
    >
      <el-skeleton v-if="detailLoading && !detail" :rows="7" animated />

      <div v-else-if="detailError" class="note note--error">
        <p class="note__title">详情加载失败：{{ detailError.code }}</p>
        <p class="note__text">{{ humanizeError(detailError) }}</p>
        <el-button size="small" class="note__action" @click="retryDetail">重试</el-button>
      </div>

      <div v-else-if="detail" class="detail">
        <div class="detail__head">
          <el-tag size="small" :type="statusMeta(detail.status).type" effect="plain">
            {{ statusMeta(detail.status).label }}
          </el-tag>
          <span class="detail__no text-mono">{{ detail.order_no }}</span>
          <span class="detail__time tnum">创建于 {{ formatTime(detail.created_at) }}</span>
        </div>

        <el-descriptions :column="2" size="small" border class="detail__desc">
          <el-descriptions-item label="申请人">{{ detail.applicant || '—' }}</el-descriptions-item>
          <el-descriptions-item label="设备型号">{{ detail.device_model || '—' }}</el-descriptions-item>
          <el-descriptions-item label="用途">{{ detail.purpose || '—' }}</el-descriptions-item>
          <el-descriptions-item label="更新时间">{{ formatTime(detail.updated_at) }}</el-descriptions-item>
          <el-descriptions-item label="合计金额">
            <span class="tnum">{{ amountText(detail.total_amount) }}</span>
            <span class="detail__currency">{{ detail.currency }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="明细条数">
            <span class="tnum">{{ detail.items.length }} 条</span>
          </el-descriptions-item>
        </el-descriptions>

        <h3 class="detail__section">明细</h3>
        <el-table :data="detail.items" size="small">
          <el-table-column prop="code" label="编码" width="126">
            <template #default="{ row }">
              <span class="text-mono">{{ row.code }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="part" label="名称" min-width="150" show-overflow-tooltip />
          <el-table-column label="数量" width="78">
            <template #default="{ row }">
              <span class="tnum">{{ row.qty }} {{ row.unit }}</span>
            </template>
          </el-table-column>
          <el-table-column label="单价" width="112">
            <template #default="{ row }">
              <span class="tnum">{{ amountText(row.unit_price) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="金额" width="112">
            <template #default="{ row }">
              <span class="tnum">{{ amountText(row.amount) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="替代件" width="86">
            <template #default="{ row }">
              <el-tag v-if="row.is_substitute" size="small" type="warning" effect="plain">
                替代件
              </el-tag>
              <span v-else class="text-secondary">主件</span>
            </template>
          </el-table-column>
          <el-table-column label="兼容性依据" min-width="200" show-overflow-tooltip>
            <template #default="{ row }">
              <span :class="row.is_substitute ? 'basis basis--on' : 'basis'">
                {{ row.is_substitute ? row.basis || '（台账未提供）' : '—' }}
              </span>
            </template>
          </el-table-column>
          <template #empty>该申请单没有明细（草稿购物车可以被清空）</template>
        </el-table>

        <!-- 合计用后端返回的 total_amount，前端不累加明细金额 -->
        <div class="detail__sum">
          <span class="detail__sum-count tnum">共 {{ detail.items.length }} 条明细</span>
          <span class="detail__sum-amount tnum">
            合计 {{ amountText(detail.total_amount) }}
            <span class="detail__currency">{{ detail.currency }}</span>
          </span>
        </div>
        <p class="hint">合计金额由后端按台账单价计算并汇总，本页不做任何金额累加。</p>

        <h3 class="detail__section">
          审计记录（note）
          <span class="detail__section-hint">每个动作由后端追加一行，含操作人与原因</span>
        </h3>
        <div v-if="noteLines.length" class="note-lines">
          <p v-for="(line, index) in noteLines" :key="index" class="note-lines__item">{{ line }}</p>
        </div>
        <p v-else class="hint">暂无审计记录。</p>

        <!-- ------------------------------------------------------- 动作区 -->
        <h3 class="detail__section">
          可执行动作
          <span class="detail__section-hint">由后端 allowed_actions 给出，前端不判断状态机</span>
        </h3>

        <div class="actions">
          <el-input
            v-model="operator"
            size="small"
            class="actions__operator"
            placeholder="操作人（选填，写入审计行）"
            clearable
          />

          <div v-if="directActions.length" class="actions__row">
            <template v-for="action in directActions" :key="action">
              <el-button
                size="small"
                type="primary"
                :loading="actionBusy"
                @click="onDirectAction(action)"
              >
                {{ actionLabel(action) }}
              </el-button>
            </template>
          </div>
          <p v-else-if="!canReject && !canCancel" class="hint">
            当前状态（{{ statusMeta(detail.status).label }}）没有可执行动作，本单为终态，只读。
          </p>

          <!-- 驳回：审批人给出反对意见 -->
          <div v-if="canReject" class="action-box action-box--reject">
            <div class="action-box__title">驳回（审批不通过）</div>
            <p class="action-box__desc">
              用于已提交 / 已确认的申请：订单进入<strong>「已驳回」</strong>，需要重新走一遍流程。
            </p>
            <div class="action-box__row">
              <el-input
                v-model="rejectReason"
                size="small"
                placeholder="驳回原因，必填，如：预算未批 / 型号不符"
                clearable
              />
              <el-button
                size="small"
                type="danger"
                :loading="actionBusy"
                @click="onReject"
              >
                {{ actionLabel('reject') }}
              </el-button>
            </div>
          </div>

          <!-- 撤销：申请人自己作废（reason 带 cancel: 前缀） -->
          <div v-if="canCancel" class="action-box action-box--cancel">
            <div class="action-box__title">撤销申请（申请人作废）</div>
            <p class="action-box__desc">
              用于草稿 / 已提交：订单进入<strong>「已撤销」</strong>。与「驳回」的区别：撤销是申请人自己
              撤回，驳回是审批不通过；两者调用同一个后端端点，撤销的 reason 会补上
              <code>cancel: </code>前缀（例：<code>cancel: 计划变更</code>）。
            </p>
            <div class="action-box__row">
              <el-input
                v-model="cancelReason"
                size="small"
                placeholder="撤销原因，必填，如：计划变更（前缀由本页自动补）"
                clearable
              />
              <el-button size="small" :loading="actionBusy" @click="onCancel">
                {{ actionLabel('cancel') }}
              </el-button>
            </div>
          </div>

          <p class="hint">
            人工确认（approve）是系统唯一的确认点：确认后才会出现收货与结算动作。
            本系统不向供应商发起任何真实下单，结算也只登记台账、不做财务过账。
          </p>
        </div>
      </div>
    </el-drawer>

    <!-- -------------------------------------------------------- 结算小对话框 -->
    <el-dialog v-model="settleVisible" title="登记结算" width="440px">
      <el-form label-width="88px" size="small">
        <el-form-item label="结算金额">
          <el-input-number
            v-model="settleForm.amount"
            :min="0"
            :precision="2"
            :step="100"
            controls-position="right"
            placeholder="留空 = 用订单金额"
            class="settle__amount"
          />
        </el-form-item>
        <el-form-item label="结算方式">
          <el-input v-model="settleForm.method" placeholder="默认「月结」" />
        </el-form-item>
        <el-form-item label="发票号">
          <el-input v-model="settleForm.invoiceNo" placeholder="选填，如 INV-20260920-77" />
        </el-form-item>
        <el-form-item label="经办人">
          <el-input v-model="settleForm.operator" placeholder="选填" />
        </el-form-item>
      </el-form>

      <p class="hint">
        金额留空即采用订单金额；同一订单重复结算会被后端拒绝（ALREADY_SETTLED）。
        本次登记<strong>只写结算台账，不做财务过账</strong>。
      </p>

      <template #footer>
        <el-button size="small" @click="settleVisible = false">取消</el-button>
        <el-button size="small" type="primary" :loading="settleSubmitting" @click="submitSettle">
          确认登记
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.proc {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  background: var(--surface-1);
}

.proc__inner {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  width: 100%;
  max-width: 1280px;
  padding: var(--sp-6) var(--sp-5) var(--sp-10);
  margin: 0 auto;
}

.proc__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-4);
}

.proc__head-text {
  min-width: 0;
}

.proc__title {
  margin: 0;
  font-size: var(--fs-xl);
  font-weight: 650;
  letter-spacing: -0.4px;
  color: var(--ink-900);
}

.proc__desc {
  max-width: 940px;
  margin: var(--sp-1) 0 0;
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--ink-500);
}

.proc__desc strong {
  color: var(--warn-600);
}

.proc__tabs :deep(.el-tabs__item) {
  font-size: var(--fs-base);
}

/* ------------------------------------------------------------------ 面板 */
.panel {
  background: rgba(22, 27, 34, 0.72);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(0, 212, 255, 0.12);
  border-radius: var(--r-lg);
  box-shadow: var(--sh-sm);
}

.panel__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-4) var(--sp-5);
  border-bottom: 1px solid var(--line-1);
}

.panel__head-text {
  min-width: 0;
}

.panel__title {
  margin: 0;
  font-size: var(--fs-base);
  font-weight: 600;
  color: var(--ink-900);
}

.panel__desc {
  margin: 3px 0 0;
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--ink-500);
}

.panel__body {
  padding: var(--sp-5);
}

.row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  align-items: center;
  margin-bottom: var(--sp-4);
}

.row__field {
  max-width: 260px;
}

.row__field--sm {
  width: 200px;
}

.count {
  padding: 1px 7px;
  font-size: 11px;
  font-weight: 600;
  color: var(--ink-500);
  background: var(--surface-2);
  border-radius: var(--r-pill);
}

.hint {
  margin: 6px 0 0;
  font-size: var(--fs-xs);
  line-height: 1.65;
  color: var(--ink-400);
}

.hint strong {
  color: var(--warn-600);
}

.empty {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  align-items: center;
  padding: var(--sp-4) 0;
  font-size: var(--fs-sm);
  color: var(--ink-500);
}

.note {
  padding: 10px var(--sp-4);
  margin-bottom: var(--sp-4);
  background: var(--danger-50);
  border: 1px solid rgba(248, 113, 113, 0.32);
  border-radius: var(--r-md);
}

.note__title {
  margin: 0 0 3px;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--danger-600);
}

.note__text {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--ink-500);
}

.note__action {
  margin-top: var(--sp-2);
}

.pager {
  justify-content: flex-end;
  margin-top: var(--sp-3);
}

/* -------------------------------------------------------------- 结算总额 */
.tally {
  margin-bottom: var(--sp-4);
}

.tally__head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-4) var(--sp-5);
}

.tally__label {
  font-size: var(--fs-xs);
  color: var(--ink-500);
}

.tally__value {
  margin-top: 4px;
  font-size: var(--fs-xl);
  font-weight: 650;
  color: var(--brand-400);
}

.tally__currency {
  margin-left: 6px;
  font-size: var(--fs-xs);
  font-weight: 400;
  color: var(--ink-400);
}

.tally__side {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: var(--sp-1);
}

/* ------------------------------------------------------------------ 详情 */
.detail__head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-3);
  margin-bottom: var(--sp-4);
}

.detail__no {
  padding: 1px 7px;
  color: var(--brand-400);
  background: rgba(0, 212, 255, 0.07);
  border: 1px solid rgba(0, 212, 255, 0.16);
  border-radius: var(--r-sm);
}

.detail__time {
  margin-left: auto;
  font-size: 11px;
  color: var(--ink-400);
}

.detail__desc {
  margin-bottom: var(--sp-5);
}

.detail__currency {
  margin-left: 6px;
  font-size: var(--fs-xs);
  color: var(--ink-400);
}

.detail__section {
  margin: var(--sp-5) 0 var(--sp-3);
  font-size: var(--fs-base);
  font-weight: 600;
  color: var(--ink-900);
}

.detail__section-hint {
  margin-left: var(--sp-2);
  font-size: var(--fs-xs);
  font-weight: 400;
  color: var(--ink-400);
}

/* 替代件依据是准入条件，用琥珀色把它从普通文本里挑出来 */
.basis {
  font-size: var(--fs-xs);
  color: var(--ink-500);
}

.basis--on {
  color: var(--warn-600);
}

/* 明细小计：右对齐贴着表格下沿，与表格里的金额列同一视觉基线 */
.detail__sum {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  margin-top: var(--sp-2);
  background: var(--surface-2);
  border: 1px solid var(--line-1);
  border-radius: var(--r-md);
}

.detail__sum-count {
  font-size: var(--fs-xs);
  color: var(--ink-500);
}

.detail__sum-amount {
  font-size: var(--fs-md);
  font-weight: 650;
  color: var(--brand-400);
}

.note-lines {
  padding: var(--sp-3);
  background: var(--surface-0);
  border: 1px solid var(--line-1);
  border-radius: var(--r-md);
}

.note-lines__item {
  margin: 0 0 4px;
  font-size: var(--fs-xs);
  line-height: 1.7;
  color: var(--ink-700);
  word-break: break-word;
}

.note-lines__item:last-child {
  margin-bottom: 0;
}

/* ---------------------------------------------------------------- 动作区 */
.actions {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-4);
  background: var(--surface-0);
  border: 1px solid var(--line-1);
  border-radius: var(--r-md);
}

.actions__operator {
  max-width: 300px;
}

.actions__row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
}

/* 驳回 / 撤销各自成块：两个动作共用后端端点，界面上必须一眼分得清 */
.action-box {
  padding: var(--sp-3) var(--sp-4);
  border: 1px solid;
  border-radius: var(--r-md);
}

.action-box--reject {
  background: var(--danger-50);
  border-color: rgba(248, 113, 113, 0.32);
}

.action-box--cancel {
  background: var(--surface-2);
  border-color: var(--line-1);
}

.action-box__title {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--ink-900);
}

.action-box__desc {
  margin: 3px 0 var(--sp-3);
  font-size: var(--fs-xs);
  line-height: 1.7;
  color: var(--ink-500);
}

.action-box__desc strong {
  color: var(--ink-900);
}

.action-box__desc code {
  padding: 0 4px;
  font-family: 'JetBrains Mono', Consolas, Menlo, monospace;
  font-size: 11px;
  color: var(--brand-400);
  background: var(--surface-1);
  border-radius: var(--r-sm);
}

.action-box__row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  align-items: center;
}

/* ------------------------------------------------------------------ 对话框 */
.settle__amount {
  width: 100%;
}

@media (max-width: 900px) {
  .proc__head {
    flex-direction: column;
  }

  .tally__side {
    align-items: flex-start;
  }
}
</style>
