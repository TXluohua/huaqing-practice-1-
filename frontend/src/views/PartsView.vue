<script setup lang="ts">
/**
 * 备件商城页（业务组页面，**与问答链路完全独立** —— 不用会话 / SSE / qa_id）。
 *
 * 页面两块：
 *   1. 左侧：台账目录（设备型号 + 关键词筛选、分页）+ 详情抽屉（含替代件依据与禁止替代清单）；
 *   2. 右侧：购物车。
 *
 * 三条业务边界在界面上都写明了（不是可选项）：
 *   * 「加入购物车」只拟一张**内部采购申请单**，**不对供应商直接下单**；
 *   * 替代件必须带台账里的兼容性依据，前端不编依据（无依据后端 400 SUBSTITUTE_BASIS_REQUIRED）；
 *   * 金额一律用后端返回的 `total_amount`，前端**不自己累加**（价格由后端回台账取）。
 *
 * 购物车 = `draft` 状态的采购申请单（后端不额外建表）：
 *   进页面先 `listOrders({status:'draft',limit:1})` 复用已有草稿单；没有草稿单时**不预先创建**——
 *   `POST /parts/orders` 的 `items` 是 `min_length=1`（空明细建单会被 422 拒绝），
 *   因此第一件备件加入购物车时才建单，之后走 `addCartItem` 累加。
 *   草稿单可以被清空（空车是正常状态），但**空车不能提交**（400 EMPTY_ORDER，后端有意保护）。
 *
 * 提交后明细即冻结（再加/改/删一律 409 INVALID_STATE），因此编辑控件统一由
 * `allowed_actions` 里是否还有 `submit` 驱动；后续流转全部在「采购结算」页完成。
 */
import { Refresh, Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'

import { formatTime, humanizeError, isApiError, type ApiError } from '@/api/http'
import {
  addCartItem,
  createOrder,
  getPart,
  listCatalog,
  listOrders,
  removeCartItem,
  stockText,
  submitOrder,
  updateCartItem,
  type OrderItem,
  type OrderItemRequest,
  type PartItem,
  type PartOrder,
  type SubstitutePart,
} from '@/api/parts'

const router = useRouter()

// ------------------------------------------------------------------ 目录筛选

const filter = reactive({ deviceModel: 'Etcher-A', keyword: '' })
const parts = ref<PartItem[]>([])
const total = ref(0)
const limit = ref(20)
const offset = ref(0)
const listLoading = ref(false)
const listError = ref<ApiError | null>(null)

async function loadCatalog(): Promise<void> {
  listLoading.value = true
  listError.value = null
  try {
    const response = await listCatalog({
      device_model: filter.deviceModel.trim() || undefined,
      keyword: filter.keyword.trim() || undefined,
      limit: limit.value,
      offset: offset.value,
    })
    parts.value = response.items
    total.value = response.total
  } catch (error) {
    // 失败时清空列表并渲染错误态：留着上一批数据会让人以为「筛选生效了」
    parts.value = []
    total.value = 0
    listError.value = isApiError(error) ? error : null
  } finally {
    listLoading.value = false
  }
}

function onSearch(): void {
  offset.value = 0
  void loadCatalog()
}

function onReset(): void {
  filter.deviceModel = 'Etcher-A'
  filter.keyword = ''
  offset.value = 0
  void loadCatalog()
}

function onPageChange(target: number): void {
  offset.value = (target - 1) * limit.value
  void loadCatalog()
}

// ------------------------------------------------------------------ 详情抽屉

const drawerVisible = ref(false)
const detailCode = ref('')
const detailPart = ref<PartItem | null>(null)
const detailLoading = ref(false)
const detailError = ref<ApiError | null>(null)

/** 打开详情：列表行里已有大部分字段，但仍以 `GET /parts/{code}` 为准（替代件/禁止清单是详情才全） */
async function openDetail(code: string): Promise<void> {
  drawerVisible.value = true
  detailCode.value = code
  detailPart.value = null
  detailError.value = null
  detailLoading.value = true
  try {
    detailPart.value = await getPart(code)
  } catch (error) {
    detailError.value = isApiError(error) ? error : null
  } finally {
    detailLoading.value = false
  }
}

function reloadDetail(): void {
  if (detailCode.value) void openDetail(detailCode.value)
}

// ------------------------------------------------------------------ 展示辅助

/** 单价为 0 表示台账未提供价格 —— 不能显示成「0 元」 */
function priceText(price: number | null | undefined): string {
  if (price === null || price === undefined || price === 0) return '台账未提供'
  return `¥ ${price.toFixed(2)}`
}

/** 缺货判定用 `stock === 0`（此时后端也标 low） */
function stockTagType(part: { stock: number; stock_status: string }): 'danger' | 'warning' | 'success' {
  if (part.stock <= 0) return 'danger'
  return part.stock_status === 'low' ? 'warning' : 'success'
}

/** 替代件的库存文案（`SubstitutePart` 不是 `PartItem`，不走 stockText） */
function substituteStockText(part: SubstitutePart): string {
  if (part.stock <= 0) return `缺货（${part.lead_time_days} 天到货）`
  return `${part.stock} ${part.unit}`
}

// ------------------------------------------------------------------ 购物车

const cart = ref<PartOrder | null>(null)
const cartLoading = ref(false)
const cartBusy = ref(false)
const cartError = ref<ApiError | null>(null)
const applicant = ref('')
const purpose = ref('')
const submitting = ref(false)

/** 草稿单才能改明细：用 `allowed_actions` 判断，不在前端硬编码状态机 */
const cartEditable = computed(() => cart.value?.allowed_actions.includes('submit') === true)

const cartCount = computed(() => cart.value?.items.length ?? 0)

/** 审计用操作人（选填，会写进订单 note 的审计行） */
function operatorName(): string | undefined {
  return applicant.value.trim() || undefined
}

async function loadCart(): Promise<void> {
  cartLoading.value = true
  cartError.value = null
  try {
    const response = await listOrders({ status: 'draft', limit: 1 })
    const draft = response.items[0] ?? null
    cart.value = draft
    if (draft) {
      if (!applicant.value) applicant.value = draft.applicant
      if (!purpose.value) purpose.value = draft.purpose
    }
  } catch (error) {
    cart.value = null
    cartError.value = isApiError(error) ? error : null
  } finally {
    cartLoading.value = false
  }
}

/**
 * 建一张空购物车。
 *
 * 后端允许零明细草稿单（`POST /api/parts/orders` 的 `items` 可为空），
 * 所以购物车不必等第一件备件才存在：用户可以先建车、再慢慢挑。
 * 空车**不能提交**（提交会收到 400 `EMPTY_ORDER`），这一点在界面上写明了。
 */
async function createEmptyCart(): Promise<void> {
  if (cartBusy.value) return
  // 已经有一张可改的空车就直接复用，避免连点产生一串空草稿单
  if (cart.value && cartEditable.value && cart.value.items.length === 0) {
    ElMessage.info(`已有一张空购物车（${cart.value.order_no}），直接加入备件即可。`)
    return
  }
  cartBusy.value = true
  try {
    cart.value = await createOrder({
      items: [],
      device_model: filter.deviceModel.trim() || undefined,
      purpose: purpose.value.trim() || undefined,
      applicant: applicant.value.trim() || undefined,
      note: '由备件商城新建空购物车',
    })
    ElMessage.success(`已建立空购物车 ${cart.value.order_no}，可以逐件加入备件`)
  } catch (error) {
    ElMessage.error(humanizeError(error))
  } finally {
    cartBusy.value = false
  }
}

/**
 * 加入购物车。
 *
 * 没有草稿单（或上一张已提交）时先建单，把这一件作为初始明细 —— 一条请求搞定。
 * 也可以先用「新建空购物车」建一张空车（见 createEmptyCart）。
 */
async function addToCart(code: string, isSubstitute = false): Promise<void> {
  if (cartBusy.value) return
  const payload: OrderItemRequest = { code, qty: 1, operator: operatorName() }
  if (isSubstitute) payload.is_substitute = true

  cartBusy.value = true
  try {
    if (!cart.value || !cartEditable.value) {
      cart.value = await createOrder({
        items: [payload],
        device_model: filter.deviceModel.trim() || undefined,
        purpose: purpose.value.trim() || undefined,
        applicant: applicant.value.trim() || undefined,
        note: '由备件商城加入购物车建立',
      })
      ElMessage.success(`已建立草稿采购申请单 ${cart.value.order_no}`)
    } else {
      cart.value = await addCartItem(cart.value.id, payload)
      ElMessage.success(isSubstitute ? '替代件已加入购物车（依据随单保存）' : '已加入购物车')
    }
  } catch (error) {
    ElMessage.error(humanizeError(error))
  } finally {
    cartBusy.value = false
  }
}

async function onQtyChange(item: OrderItem, value: number | undefined): Promise<void> {
  const order = cart.value
  if (!order || !cartEditable.value || cartBusy.value) return

  // el-input-number 在清空输入时会给出 undefined，这里回退到原数量，避免发出非法请求
  const next = Math.max(1, Math.min(999, Math.round(Number(value))))
  const qty = Number.isFinite(next) ? next : item.qty
  if (qty === item.qty) return

  cartBusy.value = true
  try {
    cart.value = await updateCartItem(order.id, item.code, { qty, operator: operatorName() })
  } catch (error) {
    ElMessage.error(humanizeError(error))
    // 改量失败时把界面拉回后端真实数量，别让输入框停在一个没生效的数字上
    await loadCart()
  } finally {
    cartBusy.value = false
  }
}

async function onRemoveItem(item: OrderItem): Promise<void> {
  const order = cart.value
  if (!order || cartBusy.value) return

  cartBusy.value = true
  try {
    cart.value = await removeCartItem(order.id, item.code, { operator: operatorName() })
    ElMessage.success(`已移出：${item.part || item.code}`)
  } catch (error) {
    ElMessage.error(humanizeError(error))
  } finally {
    cartBusy.value = false
  }
}

/** 提交采购申请（**不是给供应商下单**）。空车提交会拿到 400 EMPTY_ORDER，用 humanizeError 提示即可。 */
async function onSubmitOrder(): Promise<void> {
  const order = cart.value
  if (!order) {
    ElMessage.warning('购物车还是空的，先从左侧加入备件。')
    return
  }
  // 申请人必填：提交要有人负责（同时作为审计行的 operator 传给后端）
  if (!applicant.value.trim()) {
    ElMessage.warning('请先填写申请人，再提交采购申请。')
    return
  }

  submitting.value = true
  try {
    const response = await submitOrder(order.id, { operator: operatorName() })
    cart.value = response
    ElMessage.success(`已提交采购申请 ${response.order_no}，等待人工确认后收货与结算。`)
  } catch (error) {
    ElMessage.error(humanizeError(error))
    await loadCart()
  } finally {
    submitting.value = false
  }
}

function goProcurement(): void {
  void router.push('/procurement')
}

onMounted(() => {
  void loadCatalog()
  void loadCart()
})
</script>

<template>
  <div class="parts">
    <div class="parts__inner">
      <header class="parts__head">
        <div class="parts__head-text">
          <h1 class="parts__title">备件商城</h1>
          <p class="parts__desc">
            库存、单价与到货周期取自备件台账；替代件必须带兼容性依据。加入购物车只是拟一张内部
            <strong>采购申请单</strong>——<strong>不会向供应商直接下单</strong>，仍需人工确认后才收货与结算。
          </p>
        </div>
        <el-button size="small" @click="goProcurement">采购申请单 / 结算台账</el-button>
      </header>

      <div class="parts__cols">
        <!-- ------------------------------------------------------------ 目录 -->
        <section class="panel parts__main">
          <div class="panel__head">
            <div class="panel__head-text">
              <h2 class="panel__title">备件目录</h2>
              <p class="panel__desc">
                按设备型号筛出该机型与「通用」件；缺货按库存 0 判定，并给出到货天数预期。
              </p>
            </div>
            <span v-if="total" class="count tnum">{{ total }} 条</span>
          </div>

          <div class="panel__body">
            <div class="row">
              <el-input
                v-model="filter.deviceModel"
                size="small"
                class="row__field--sm"
                placeholder="设备型号，如 Etcher-A"
                clearable
              />
              <el-input
                v-model="filter.keyword"
                size="small"
                class="row__field"
                placeholder="关键词，如 O-ring"
                clearable
                @keyup.enter="onSearch"
              />
              <el-button size="small" type="primary" :icon="Search" @click="onSearch">搜索</el-button>
              <el-button size="small" :icon="Refresh" @click="onReset">重置</el-button>
            </div>

            <div v-if="listError" class="note note--error">
              <p class="note__title">目录加载失败：{{ listError.code }}</p>
              <p class="note__text">{{ humanizeError(listError) }}</p>
            </div>

            <el-table v-else v-loading="listLoading" :data="parts" size="small" class="catalog">
              <el-table-column prop="code" label="编码" width="132">
                <template #default="{ row }">
                  <span class="text-mono">{{ row.code }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="part" label="名称" min-width="170" show-overflow-tooltip />
              <el-table-column prop="spec" label="规格" min-width="150" show-overflow-tooltip />
              <el-table-column label="库存" width="152">
                <template #default="{ row }">
                  <el-tag size="small" :type="stockTagType(row)" effect="plain">
                    {{ stockText(row) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="location" label="库位" width="100" show-overflow-tooltip />
              <el-table-column label="到货天数" width="92">
                <template #default="{ row }">
                  <span class="tnum">{{ row.lead_time_days }} 天</span>
                </template>
              </el-table-column>
              <el-table-column label="单价" width="120">
                <template #default="{ row }">
                  <span class="tnum">{{ priceText(row.price_cny) }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="supplier" label="供应商" width="150" show-overflow-tooltip />
              <el-table-column label="替代件" width="86">
                <template #default="{ row }">
                  <span class="tnum" :class="{ 'text-secondary': row.substitutes.length === 0 }">
                    {{ row.substitutes.length }} 项
                  </span>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="152" fixed="right">
                <template #default="{ row }">
                  <el-button size="small" link type="primary" @click="openDetail(row.code)">
                    详情
                  </el-button>
                  <el-button
                    size="small"
                    link
                    type="primary"
                    :disabled="cartBusy"
                    @click="addToCart(row.code)"
                  >
                    加入购物车
                  </el-button>
                </template>
              </el-table-column>
              <template #empty>没有匹配的备件，试试换型号或关键词</template>
            </el-table>

            <el-pagination
              v-if="total > limit"
              class="catalog__pager"
              layout="prev, pager, next, total"
              size="small"
              :current-page="Math.floor(offset / limit) + 1"
              :page-size="limit"
              :total="total"
              @current-change="onPageChange"
            />
          </div>
        </section>

        <!-- ---------------------------------------------------------- 购物车 -->
        <aside class="panel parts__cart">
          <div class="panel__head">
            <div class="panel__head-text">
              <h2 class="panel__title">购物车</h2>
              <p class="panel__desc">
                提交后生成<strong>采购申请单</strong>（不对供应商直接下单），明细随即冻结；
                「新建购物车」会另开一张草稿单，原草稿单仍留在采购页。
              </p>
            </div>
            <el-button
              size="small"
              link
              type="primary"
              :loading="cartBusy"
              @click="createEmptyCart"
            >
              新建空购物车
            </el-button>
          </div>

          <div class="panel__body cart__body">
            <el-skeleton v-if="cartLoading" :rows="4" animated />

            <div v-else-if="cartError" class="note note--error">
              <p class="note__title">购物车加载失败：{{ cartError.code }}</p>
              <p class="note__text">{{ humanizeError(cartError) }}</p>
              <el-button size="small" class="note__action" @click="loadCart">重试</el-button>
            </div>

            <template v-else>
              <!--
                申请人 / 用途放在明细之上：申请表头（applicant / purpose / device_model）只在
                **建单那一次**写入，之后没有单独的改表头接口 —— 所以「先填申请人，再加入第一件备件」
                才是能把申请人记到申请单上的顺序。
              -->
              <div v-if="!cart || cartEditable" class="cart__form">
                <el-input
                  v-model="applicant"
                  size="small"
                  placeholder="申请人（提交前必填）"
                  clearable
                />
                <el-input
                  v-model="purpose"
                  size="small"
                  placeholder="用途 / 关联工单（选填，建单时写入）"
                  clearable
                />
                <p class="hint">
                  申请人会写进申请单表头（加入首件备件时建单），提交采购申请前必须填写。
                </p>
              </div>

              <!-- 还没有购物车：直接给一个「建空车」的入口，而不是让用户去猜要等第一件备件 -->
              <el-empty
                v-if="!cart"
                :image-size="70"
                description="还没有购物车：可以新建一张空车，也可以直接从左侧加入备件"
              >
                <el-button
                  size="small"
                  type="primary"
                  :loading="cartBusy"
                  @click="createEmptyCart"
                >
                  新建空购物车
                </el-button>
              </el-empty>

              <template v-if="cart">
                <div class="cart__meta">
                  <span class="cart__no text-mono">{{ cart.order_no }}</span>
                  <el-tag size="small" type="info" effect="plain">草稿（未提交）</el-tag>
                  <span class="cart__time tnum">{{ formatTime(cart.updated_at) }}</span>
                </div>

                <el-empty
                  v-if="cart.items.length === 0"
                  :image-size="70"
                  description="购物车是空的：从左侧「加入购物车」开始（空车不能提交）"
                />

                <ul v-else class="cart__list">
                  <li v-for="item in cart.items" :key="item.code" class="cart-item">
                    <div class="cart-item__head">
                      <span class="cart-item__name">{{ item.part || item.code }}</span>
                      <el-tag v-if="item.is_substitute" size="small" type="warning" effect="plain">
                        替代件
                      </el-tag>
                    </div>
                    <div class="cart-item__sub text-mono">{{ item.code }}</div>

                    <div class="cart-item__row">
                      <el-input-number
                        :model-value="item.qty"
                        size="small"
                        :min="1"
                        :max="999"
                        :disabled="!cartEditable || cartBusy"
                        controls-position="right"
                        @change="onQtyChange(item, $event)"
                      />
                      <span class="cart-item__price tnum">
                        {{ priceText(item.unit_price) }} × {{ item.qty }}
                      </span>
                      <span class="cart-item__amount tnum">{{ priceText(item.amount) }}</span>
                    </div>

                    <!-- 替代件的兼容性依据：下单前必须看得见 -->
                    <p v-if="item.is_substitute" class="cart-item__basis">
                      依据：{{ item.basis || '（台账未提供，后端会拒绝）' }}
                    </p>

                    <div class="cart-item__foot">
                      <span class="cart-item__stock">
                        库存 {{ item.stock }} {{ item.unit }} · {{ item.lead_time_days }} 天到货
                      </span>
                      <el-button
                        size="small"
                        link
                        type="danger"
                        :disabled="!cartEditable || cartBusy"
                        @click="onRemoveItem(item)"
                      >
                        移出
                      </el-button>
                    </div>
                  </li>
                </ul>

                <div v-if="cart.items.length > 0" class="cart__total">
                  <span class="cart__total-label">合计（后端汇总）</span>
                  <span class="cart__total-value tnum">
                    {{ priceText(cart.total_amount) }} {{ cart.currency }}
                  </span>
                </div>

                <template v-if="cartEditable">
                  <el-button
                    class="cart__submit"
                    type="primary"
                    size="small"
                    :loading="submitting"
                    @click="onSubmitOrder"
                  >
                    提交采购申请
                  </el-button>
                  <p class="hint">
                    提交后明细冻结，需在采购页由人工确认（approve）；空购物车提交会被后端拒绝（EMPTY_ORDER），
                    这是有意的保护。提交只生成内部采购申请单，<strong>不对供应商下单</strong>。
                  </p>
                </template>

                <template v-else>
                  <p class="hint">
                    该申请单已提交（单号 {{ cart.order_no }}），明细已冻结，改数量 / 移出入口已关闭。
                    请到采购结算页跟踪状态；要继续选件请点「新建空购物车」。
                  </p>
                  <el-button class="cart__submit" size="small" type="primary" plain @click="goProcurement">
                    去采购结算页
                  </el-button>
                </template>
              </template>

              <template v-else>
                <el-empty
                  :image-size="70"
                  description="还没有草稿申请单：加入第一件备件时会自动建立"
                />
                <p class="hint">
                  购物车就是一张草稿采购申请单，刷新不丢、金额由后端算；提交后进入人工确认流程。
                </p>
              </template>
            </template>

            <p v-if="cartCount > 0" class="hint">
              共 {{ cartCount }} 条明细。库存与价格以后端台账为准，本页不做任何金额计算。
            </p>
          </div>
        </aside>
      </div>
    </div>

    <!-- ------------------------------------------------------------ 详情抽屉 -->
    <el-drawer
      v-model="drawerVisible"
      size="560px"
      :title="detailPart ? `${detailPart.part}（${detailPart.code}）` : '备件详情'"
    >
      <el-skeleton v-if="detailLoading" :rows="7" animated />

      <div v-else-if="detailError" class="note note--error">
        <p class="note__title">详情加载失败：{{ detailError.code }}</p>
        <p class="note__text">{{ humanizeError(detailError) }}</p>
        <el-button size="small" class="note__action" @click="reloadDetail">重试</el-button>
      </div>

      <div v-else-if="detailPart" class="detail">
        <!--
          禁止替代清单：安全信息，**必须是页面里最显眼的一块**（红色 + 常显 + 不折叠），
          每一条都是后端给的成品句「禁止替代：X —— 原因」，前端不再拼格式。
        -->
        <el-alert
          v-if="detailPart.forbidden.length"
          type="error"
          :closable="false"
          show-icon
          title="禁止替代清单（安全信息）"
          class="detail__forbid"
        >
          <ul class="forbid-list">
            <li v-for="(line, index) in detailPart.forbidden" :key="index">{{ line }}</li>
          </ul>
        </el-alert>
        <el-alert
          v-else
          type="info"
          :closable="false"
          show-icon
          title="台账未登记禁止替代项"
          class="detail__forbid"
        />

        <el-descriptions :column="2" size="small" border class="detail__desc">
          <el-descriptions-item label="编码">
            <span class="text-mono">{{ detailPart.code }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="设备型号">{{ detailPart.device_model || '通用' }}</el-descriptions-item>
          <el-descriptions-item label="规格">{{ detailPart.spec || '—' }}</el-descriptions-item>
          <el-descriptions-item label="库位">{{ detailPart.location || '—' }}</el-descriptions-item>
          <el-descriptions-item label="库存">
            <el-tag size="small" :type="stockTagType(detailPart)" effect="plain">
              {{ stockText(detailPart) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="到货天数">
            <span class="tnum">{{ detailPart.lead_time_days }} 天</span>
          </el-descriptions-item>
          <el-descriptions-item label="单价">
            <span class="tnum">{{ priceText(detailPart.price_cny) }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="供应商">{{ detailPart.supplier || '—' }}</el-descriptions-item>
        </el-descriptions>

        <div class="detail__actions">
          <el-button
            type="primary"
            size="small"
            :disabled="cartBusy"
            @click="addToCart(detailPart.code)"
          >
            加入购物车（本件）
          </el-button>
          <span class="hint">加入购物车只是拟内部采购申请单，不向供应商下单。</span>
        </div>

        <h3 class="detail__section">
          替代件（{{ detailPart.substitutes.length }}）
          <span class="detail__section-hint">依据来自台账原文，需保留依据才能下单</span>
        </h3>

        <el-empty
          v-if="detailPart.substitutes.length === 0"
          :image-size="60"
          description="台账未登记可用替代件"
        />

        <ul v-else class="subs">
          <li v-for="sub in detailPart.substitutes" :key="sub.code" class="sub">
            <div class="sub__head">
              <span class="sub__name">{{ sub.part || sub.code }}</span>
              <span class="sub__code text-mono">{{ sub.code }}</span>
            </div>

            <div class="sub__meta">
              <span class="tnum">{{ substituteStockText(sub) }}</span>
              <span class="tnum">{{ priceText(sub.price_cny) }}</span>
              <span class="tnum">{{ sub.lead_time_days }} 天到货</span>
            </div>

            <!-- 兼容性依据原文：展示时不要省略 -->
            <p class="sub__basis">兼容性依据：{{ sub.basis || '（台账未提供 —— 无依据的替代件后端会拒绝）' }}</p>

            <el-button
              size="small"
              type="primary"
              plain
              :disabled="cartBusy"
              @click="addToCart(sub.code, true)"
            >
              加入购物车（替代件）
            </el-button>
          </li>
        </ul>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.parts {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  background: var(--surface-1);
}

.parts__inner {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  width: 100%;
  max-width: 1360px;
  padding: var(--sp-6) var(--sp-5) var(--sp-10);
  margin: 0 auto;
}

.parts__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-4);
}

.parts__head-text {
  min-width: 0;
}

.parts__title {
  margin: 0;
  font-size: var(--fs-xl);
  font-weight: 650;
  letter-spacing: -0.4px;
  color: var(--ink-900);
}

.parts__desc {
  max-width: 900px;
  margin: var(--sp-1) 0 0;
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--ink-500);
}

.parts__desc strong {
  color: var(--warn-600);
}

/* 左目录 + 右购物车；窄屏落成一列（先选件、再看车） */
.parts__cols {
  display: grid;
  gap: var(--sp-4);
  align-items: start;
  grid-template-columns: minmax(0, 1fr) 372px;
}

.parts__main,
.parts__cart {
  min-width: 0;
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

.panel__desc strong {
  color: var(--warn-600);
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
  max-width: 240px;
}

.row__field--sm {
  width: 180px;
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

/* ------------------------------------------------------------------ 目录 */
.catalog {
  margin-top: var(--sp-1);
}

.catalog__pager {
  justify-content: flex-end;
  margin-top: var(--sp-3);
}

/* ---------------------------------------------------------------- 购物车 */
/* 购物车独立滚动：它常驻在右侧，不跟随目录分页整体滚动 */
.cart__body {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  max-height: 70vh;
  overflow-y: auto;
}

.cart__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
}

.cart__no {
  padding: 1px 7px;
  color: var(--brand-400);
  background: rgba(0, 212, 255, 0.07);
  border: 1px solid rgba(0, 212, 255, 0.16);
  border-radius: var(--r-sm);
}

.cart__time {
  margin-left: auto;
  font-size: 11px;
  color: var(--ink-400);
}

.cart__list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  padding: 0;
  margin: 0;
  list-style: none;
}

.cart-item {
  padding: 10px var(--sp-3) var(--sp-3);
  background: var(--surface-0);
  border: 1px solid var(--line-1);
  border-radius: var(--r-md);
}

.cart-item__head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

.cart-item__name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--ink-900);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cart-item__sub {
  margin: 2px 0 6px;
  color: var(--ink-400);
}

.cart-item__row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

.cart-item__price {
  font-size: var(--fs-xs);
  color: var(--ink-500);
}

.cart-item__amount {
  margin-left: auto;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--ink-900);
}

/* 依据用左侧竖线「引」出来：它是替代件的准入条件，不能当成普通小字 */
.cart-item__basis {
  padding-left: var(--sp-2);
  margin: var(--sp-2) 0 0;
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--warn-600);
  border-left: 2px solid rgba(251, 191, 36, 0.45);
}

.cart-item__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
  margin-top: var(--sp-2);
}

.cart-item__stock {
  font-size: 11px;
  color: var(--ink-400);
}

.cart__total {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--sp-2);
  padding: var(--sp-3);
  background: var(--surface-2);
  border: 1px solid var(--line-1);
  border-radius: var(--r-md);
}

.cart__total-label {
  font-size: var(--fs-xs);
  color: var(--ink-500);
}

.cart__total-value {
  font-size: var(--fs-md);
  font-weight: 650;
  color: var(--brand-400);
}

.cart__form {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

.cart__submit {
  margin-top: var(--sp-3);
  width: 100%;
}

/* ------------------------------------------------------------------ 详情 */
.detail__forbid {
  margin-bottom: var(--sp-4);
  border-radius: var(--r-md);
}

.forbid-list {
  padding-left: 18px;
  margin: 4px 0 0;
}

.forbid-list li {
  font-size: var(--fs-sm);
  line-height: 1.7;
  word-break: break-word;
}

.detail__desc {
  margin-bottom: var(--sp-4);
}

.detail__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-3);
  padding-bottom: var(--sp-4);
  border-bottom: 1px solid var(--line-1);
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

.subs {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: 0;
  margin: 0;
  list-style: none;
}

.sub {
  padding: var(--sp-3) var(--sp-4);
  background: var(--surface-0);
  border: 1px solid var(--line-1);
  border-left: 2px solid var(--warn-600);
  border-radius: var(--r-md);
}

.sub__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
}

.sub__name {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--ink-900);
}

.sub__code {
  color: var(--ink-400);
}

.sub__meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-4);
  margin: var(--sp-2) 0;
  font-size: var(--fs-xs);
  color: var(--ink-500);
}

.sub__basis {
  padding-left: var(--sp-2);
  margin: 0 0 var(--sp-3);
  font-size: var(--fs-xs);
  line-height: 1.65;
  color: var(--ink-700);
  border-left: 2px solid var(--line-1);
}

@media (max-width: 1100px) {
  .parts__cols {
    grid-template-columns: minmax(0, 1fr);
  }

  .cart__body {
    max-height: none;
  }
}
</style>
