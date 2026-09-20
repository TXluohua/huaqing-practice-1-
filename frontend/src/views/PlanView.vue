<script setup lang="ts">
/**
 * 维护计划页（功能①：维护计划生成）。
 *
 * 独立性：本页只依赖 `@/api/plans` 与 `@/api/http` —— 不需要会话 id、不需要 SSE、
 * 不需要 qa_id，与问答链路（stores/chat、utils/sse、api/chat）零耦合。
 * 整条链路都是普通 JSON 请求：设备参数从表单进后端，计划条目从 /plans 读回。
 *
 * 页面四块：
 *   1. 生成表单 —— 设备参数 + top_k，两个入口：生成预览（persist=false，不落库）/ 生成并保存；
 *   2. 生成结果 —— 本次返回的条目 + **手册未覆盖项目（uncovered）**，后者必须显式展示，
 *      不能因为它不是「错误」就悄悄丢掉：它正是「手册里查不到周期」的缺口本身；
 *   3. 计划列表 —— 状态筛选 + 分页 + 依据抽屉 + 完成/跳过；
 *   4. 依据抽屉 / 完成·跳过弹窗（页面级单例，和 SourceDrawer 在问答页的位置同理）。
 *
 * 排序：列表**严格按后端返回顺序**渲染（后端已按 remaining 升序，最紧急在前）。
 * 页面不做任何二次排序，否则「最紧急在前」这条接口契约会被前端悄悄改掉。
 */
import { Document, Refresh } from '@element-plus/icons-vue'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { computed, onMounted, reactive, ref } from 'vue'

import { formatTime, humanizeError } from '@/api/http'
import {
  BASIS_LABELS,
  BASIS_UNITS,
  STATUS_META,
  completePlan,
  generatePlan,
  listPlans,
  remainingText,
  skipPlan,
  type PlanGenerateRequest,
  type PlanGenerateResponse,
  type PlanItem,
  type PlanStatus,
} from '@/api/plans'

/** 每页条数：分页用 limit/offset，页码由 offset 换算 */
const PAGE_SIZE = 10

/** 状态筛选项。文案取自 STATUS_META，避免「已到期」这类词在多处各写一遍后跑偏 */
const STATUS_FILTERS: { value: PlanStatus | ''; label: string }[] = [
  { value: '', label: '全部' },
  { value: 'planned', label: STATUS_META['planned'].label },
  { value: 'due', label: STATUS_META['due'].label },
  { value: 'done', label: STATUS_META['done'].label },
  { value: 'skipped', label: STATUS_META['skipped'].label },
]

type GenerateMode = 'preview' | 'persist'
type ActionMode = 'complete' | 'skip'
type TagType = 'primary' | 'danger' | 'success' | 'info'

interface GenerateForm {
  device_model: string
  device_code: string
  runtime_hours: number | undefined
  wafer_count: number | undefined
  /** 距上次 PM 的小时数；留空（undefined）表示让后端用累计值推算 */
  hours_since_pm: number | undefined
  wafers_since_pm: number | undefined
  top_k: number | undefined
}

// --------------------------------------------------------------------------- //
// ① 生成表单
// --------------------------------------------------------------------------- //

/**
 * 默认值取一台刻蚀机的典型读数：页面打开即可直接点「生成预览」看到效果，
 * 不必先手工填七个字段 —— 演示与排障时这一步省掉的成本最高。
 */
const generateForm = reactive<GenerateForm>({
  device_model: 'Etcher-A',
  device_code: 'ETCH-01',
  runtime_hours: 5200,
  wafer_count: 46000,
  hours_since_pm: undefined,
  wafers_since_pm: undefined,
  top_k: 8,
})

const generateRules: FormRules = {
  device_model: [
    { required: true, message: '设备型号必填：它决定命中哪些设备手册', trigger: 'blur' },
  ],
}

const generateFormRef = ref<FormInstance>()
/** null = 空闲；否则记录当前在跑的是哪个按钮，让另一个按钮同步置灰 */
const generating = ref<GenerateMode | null>(null)

const generated = ref<PlanGenerateResponse | null>(null)
/** 本次结果是否已落库（预览不落库，列表里不会出现这些条目） */
const generatedPersisted = ref(false)

// 从可空对象里取数组：模板里就不必反复做非空判断
const generatedItems = computed<PlanItem[]>(() => generated.value?.items ?? [])
const generatedUncovered = computed<string[]>(() => generated.value?.uncovered ?? [])

async function submitGenerate(persist: boolean): Promise<void> {
  const form = generateFormRef.value
  if (form) {
    try {
      await form.validate()
    } catch {
      return // 校验不通过：错误文案已标在字段下方，不再重复弹提示
    }
  }

  const deviceModel = generateForm.device_model.trim()
  if (!deviceModel) {
    ElMessage.warning('请填写设备型号')
    return
  }

  const payload: PlanGenerateRequest = {
    device_model: deviceModel,
    device_code: generateForm.device_code.trim() || undefined,
    runtime_hours: generateForm.runtime_hours,
    wafer_count: generateForm.wafer_count,
    // 留空必须发 null（而不是 0）：null = 让后端用累计值推算，0 = 「刚做完 PM」
    hours_since_pm: generateForm.hours_since_pm ?? null,
    wafers_since_pm: generateForm.wafers_since_pm ?? null,
    top_k: generateForm.top_k,
    persist,
  }

  generating.value = persist ? 'persist' : 'preview'
  try {
    const response = await generatePlan(payload)
    generated.value = response
    generatedPersisted.value = persist

    if (response.uncovered.length > 0) {
      // 用 warning 而不是 success：未覆盖项需要人工确认，是待处理事项而非纯提示
      ElMessage.warning(
        `已生成 ${response.items.length} 条计划，另有 ${response.uncovered.length} 项手册未给出周期`,
      )
    } else {
      ElMessage.success(
        persist ? `已生成并保存 ${response.items.length} 条计划` : `已生成 ${response.items.length} 条预览`,
      )
    }

    // 生成成功后刷新列表：persist=true 时新条目会出现在下方列表里
    offset.value = 0
    await loadPlans()
  } catch (error) {
    ElMessage.error(humanizeError(error))
  } finally {
    generating.value = null
  }
}

// --------------------------------------------------------------------------- //
// ② 计划列表
// --------------------------------------------------------------------------- //

const plans = ref<PlanItem[]>([])
const total = ref(0)
const offset = ref(0)
const listLoading = ref(false)
const statusFilter = ref<PlanStatus | ''>('')

const currentPage = computed(() => Math.floor(offset.value / PAGE_SIZE) + 1)

/**
 * 拉取计划列表。
 * 列表**不按设备型号过滤**：不同设备共用一个待办队列，筛选只按状态（见页面筛选器）。
 * 失败时清空列表而不是留着上一次的数据 —— 否则用户会把陈旧数据当成刷新结果。
 */
async function loadPlans(): Promise<void> {
  listLoading.value = true
  try {
    const response = await listPlans({
      status: statusFilter.value || undefined,
      limit: PAGE_SIZE,
      offset: offset.value,
    })
    plans.value = response.items
    total.value = response.total
  } catch (error) {
    plans.value = []
    total.value = 0
    ElMessage.error(humanizeError(error))
  } finally {
    listLoading.value = false
  }
}

async function onFilterChange(): Promise<void> {
  offset.value = 0
  await loadPlans()
}

async function onPageChange(page: number): Promise<void> {
  offset.value = (page - 1) * PAGE_SIZE
  await loadPlans()
}

onMounted(() => {
  // 首屏先把列表拉起来：表单是「再生成一份」，列表才是页面主体
  void loadPlans()
})

// --------------------------------------------------------------------------- //
// 展示辅助
// --------------------------------------------------------------------------- //

function basisLabel(basis: string): string {
  return BASIS_LABELS[basis] ?? basis
}

function basisUnit(basis: string): string {
  return BASIS_UNITS[basis] ?? ''
}

/** 未知状态不隐藏：原样显示状态码，配 info 色，避免整行看起来「没有状态」 */
function statusMetaOf(status: string): { label: string; type: TagType } {
  return STATUS_META[status] ?? { label: status, type: 'info' }
}

/** done / skipped 是终态：操作置灰而不是隐藏，操作列宽度才稳定 */
function isClosed(item: PlanItem): boolean {
  return item.status === 'done' || item.status === 'skipped'
}

/** 已到期的整行标红（配合下方 :deep 样式）：一屏里最先该被看到的就是这一行 */
function rowClassName({ row }: { row: PlanItem }): string {
  return row.status === 'due' ? 'plan-row--due' : ''
}

// --------------------------------------------------------------------------- //
// ③ 依据抽屉
// --------------------------------------------------------------------------- //

const evidenceVisible = ref(false)
const evidencePlan = ref<PlanItem | null>(null)
const evidenceTitle = computed(() =>
  evidencePlan.value ? `依据：${evidencePlan.value.item_name}` : '依据',
)

/** 每条计划项都必须能点开看依据：evidence 非空是后端保证的契约 */
function openEvidence(item: PlanItem): void {
  evidencePlan.value = item
  evidenceVisible.value = true
}

// --------------------------------------------------------------------------- //
// ④ 完成 / 跳过
// --------------------------------------------------------------------------- //

const actionTarget = ref<PlanItem | null>(null)
const actionMode = ref<ActionMode | null>(null)
const actionSubmitting = ref(false)
const actionForm = reactive<{ note: string; completed_value: number | undefined }>({
  note: '',
  completed_value: undefined,
})

/** 弹窗开关由 actionMode 派生：只有一个状态源，不会出现「关掉了但目标还在」 */
const actionVisible = computed<boolean>({
  get: () => actionMode.value !== null,
  set: (visible: boolean) => {
    if (!visible) actionMode.value = null
  },
})

const actionTitle = computed(() => (actionMode.value === 'skip' ? '跳过计划项' : '完成计划项'))

function openAction(item: PlanItem, mode: ActionMode): void {
  if (item.id === null || item.id === undefined) {
    // 预览结果没有落库，也就没有可操作的 id
    ElMessage.warning('该条目尚未落库（预览结果），无法标记状态')
    return
  }
  actionTarget.value = item
  actionMode.value = mode
  actionForm.note = ''
  // 完成值默认带出当前计量值：多数情况下「完成时的读数」就是此刻的读数
  actionForm.completed_value = mode === 'complete' ? item.current_value : undefined
}

async function submitAction(): Promise<void> {
  const target = actionTarget.value
  const mode = actionMode.value
  if (!target || mode === null) return

  const planId = target.id
  if (planId === null || planId === undefined) return

  const note = actionForm.note.trim()
  if (mode === 'skip' && !note) {
    // 后端对空原因会写成「未填写原因」，那等于把「为什么跳过」这个信息永久丢掉
    ElMessage.warning('请填写跳过原因（不填后端会记为「未填写原因」）')
    return
  }

  actionSubmitting.value = true
  try {
    if (mode === 'complete') {
      await completePlan(planId, {
        note: note || null,
        completed_value: actionForm.completed_value ?? null,
      })
      ElMessage.success(`已完成「${target.item_name}」`)
    } else {
      await skipPlan(planId, { note })
      ElMessage.success(`已跳过「${target.item_name}」`)
    }
    actionMode.value = null
    await loadPlans()
  } catch (error) {
    ElMessage.error(humanizeError(error))
  } finally {
    actionSubmitting.value = false
  }
}
</script>

<template>
  <div class="plan">
    <div class="plan__inner">
      <header class="plan__head">
        <h1 class="plan__title">维护计划</h1>
        <p class="plan__desc">
          按设备累计运行量对照手册周期生成待办；每条计划都能点开依据核对出处。
        </p>
      </header>

      <!-- ------------------------------------------------------- ① 生成表单 -->
      <section class="panel">
        <div class="panel__head">
          <div class="panel__head-text">
            <h2 class="panel__title">生成计划</h2>
            <p class="panel__desc">
              「生成预览」只计算不落库，确认无误后再「生成并保存」
            </p>
          </div>
        </div>

        <div class="panel__body">
          <el-form
            ref="generateFormRef"
            :model="generateForm"
            :rules="generateRules"
            label-width="132px"
            size="small"
            class="gen-form"
          >
            <div class="gen-form__grid">
              <el-form-item label="设备型号" prop="device_model">
                <el-input
                  v-model="generateForm.device_model"
                  placeholder="例如 Etcher-A"
                  clearable
                />
              </el-form-item>

              <el-form-item label="设备编号">
                <el-input
                  v-model="generateForm.device_code"
                  placeholder="例如 ETCH-01"
                  clearable
                />
              </el-form-item>

              <el-form-item label="累计运行小时">
                <el-input-number
                  v-model="generateForm.runtime_hours"
                  :min="0"
                  :step="100"
                  controls-position="right"
                  class="gen-form__num"
                />
              </el-form-item>

              <el-form-item label="累计生产片数">
                <el-input-number
                  v-model="generateForm.wafer_count"
                  :min="0"
                  :step="1000"
                  controls-position="right"
                  class="gen-form__num"
                />
              </el-form-item>

              <el-form-item label="距上次 PM（小时）">
                <el-input-number
                  v-model="generateForm.hours_since_pm"
                  :min="0"
                  :step="100"
                  controls-position="right"
                  placeholder="留空 = 用累计值"
                  class="gen-form__num"
                />
              </el-form-item>

              <el-form-item label="距上次 PM（片数）">
                <el-input-number
                  v-model="generateForm.wafers_since_pm"
                  :min="0"
                  :step="1000"
                  controls-position="right"
                  placeholder="留空 = 用累计值"
                  class="gen-form__num"
                />
              </el-form-item>

              <el-form-item label="召回条数 top_k">
                <el-input-number
                  v-model="generateForm.top_k"
                  :min="1"
                  :max="20"
                  controls-position="right"
                  class="gen-form__num"
                />
              </el-form-item>
            </div>

            <div class="gen-form__actions">
              <el-button
                :loading="generating === 'preview'"
                :disabled="generating !== null"
                @click="submitGenerate(false)"
              >
                生成预览
              </el-button>
              <el-button
                type="primary"
                :loading="generating === 'persist'"
                :disabled="generating !== null"
                @click="submitGenerate(true)"
              >
                生成并保存
              </el-button>
            </div>
          </el-form>
        </div>
      </section>

      <!-- ------------------------------------------------------- ② 生成结果 -->
      <section v-if="generated" class="panel">
        <div class="panel__head">
          <div class="panel__head-text">
            <h2 class="panel__title">生成结果</h2>
            <p class="panel__desc">
              {{ generated?.device_model }} / {{ generated?.device_code }} ·
              共 {{ generatedItems.length }} 条 ·
              <span class="text-mono">trace {{ generated?.trace_id ?? '—' }}</span>
            </p>
          </div>
          <el-tag :type="generatedPersisted ? 'success' : 'info'" size="small" effect="plain">
            {{ generatedPersisted ? '已落库' : '仅预览（未落库）' }}
          </el-tag>
        </div>

        <div class="panel__body">
          <!-- 未覆盖项必须展示：这是「手册没写周期、需要人工确认」的缺口清单 -->
          <el-alert
            v-if="generatedUncovered.length > 0"
            class="uncovered"
            type="warning"
            show-icon
            :closable="false"
            title="以下项目手册未给出周期，需人工确认"
          >
            <ul class="uncovered__list">
              <li v-for="(text, index) in generatedUncovered" :key="`${index}-${text}`">
                {{ text }}
              </li>
            </ul>
          </el-alert>
          <p v-else class="hint hint--inline">
            本次生成的项目都在手册里找到了周期依据，没有未覆盖项。
          </p>

          <!-- 预览条目在这里只读展示（未落库的条目没有 id，无法标记状态）；
               落库后的条目在下方列表里用「完成 / 跳过」操作 -->
          <el-table :data="generatedItems" size="small" class="preview">
            <el-table-column prop="item_name" label="项目名" min-width="170" show-overflow-tooltip />
            <el-table-column prop="item_type" label="类型" width="110" show-overflow-tooltip />
            <el-table-column label="计量口径" width="100">
              <template #default="{ row }">{{ basisLabel(row.cycle_basis) }}</template>
            </el-table-column>
            <el-table-column label="周期值" width="110">
              <template #default="{ row }">
                <span class="tnum">
                  {{ row.cycle_value }} {{ basisUnit(row.cycle_basis) }}
                </span>
              </template>
            </el-table-column>
            <el-table-column label="当前值" width="100">
              <template #default="{ row }">
                <span class="tnum">{{ row.current_value }}</span>
              </template>
            </el-table-column>
            <el-table-column label="剩余" width="130">
              <template #default="{ row }">{{ remainingText(row) }}</template>
            </el-table-column>
            <el-table-column label="到期日" width="150">
              <template #default="{ row }">
                <span v-if="row.due_at" class="tnum">{{ formatTime(row.due_at) }}</span>
                <span v-else class="muted">按需执行</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="92">
              <template #default="{ row }">
                <el-tag :type="statusMetaOf(row.status).type" size="small" effect="dark">
                  {{ statusMetaOf(row.status).label }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="依据" width="102">
              <template #default="{ row }">
                <el-button link type="primary" size="small" :icon="Document" @click="openEvidence(row)">
                  依据 ({{ row.evidence.length }})
                </el-button>
              </template>
            </el-table-column>
            <template #empty>
              <el-empty description="本次没有算出计划项" :image-size="70" />
            </template>
          </el-table>
        </div>
      </section>

      <!-- ------------------------------------------------------- ③ 计划列表 -->
      <section class="panel">
        <div class="panel__head">
          <div class="panel__head-text">
            <div class="panel__title-row">
              <h2 class="panel__title">计划列表</h2>
              <span v-if="total" class="plan__count tnum">{{ total }}</span>
            </div>
            <p class="panel__desc">按后端返回顺序展示（最紧急在前），本页不做二次排序</p>
          </div>

          <div class="plan__filters">
            <el-radio-group v-model="statusFilter" size="small" @change="onFilterChange">
              <el-radio-button
                v-for="option in STATUS_FILTERS"
                :key="option.value"
                :value="option.value"
              >
                {{ option.label }}
              </el-radio-button>
            </el-radio-group>
            <el-button size="small" :icon="Refresh" :loading="listLoading" @click="loadPlans">
              刷新
            </el-button>
          </div>
        </div>

        <div class="panel__body">
          <el-table
            v-loading="listLoading"
            :data="plans"
            size="small"
            class="plans"
            :row-class-name="rowClassName"
          >
            <el-table-column prop="item_name" label="项目名" min-width="180" show-overflow-tooltip />
            <el-table-column prop="item_type" label="类型" width="110" show-overflow-tooltip />
            <el-table-column label="计量口径" width="100">
              <template #default="{ row }">{{ basisLabel(row.cycle_basis) }}</template>
            </el-table-column>
            <el-table-column label="周期值" width="110">
              <template #default="{ row }">
                <span class="tnum">{{ row.cycle_value }} {{ basisUnit(row.cycle_basis) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="当前值" width="100">
              <template #default="{ row }">
                <span class="tnum">{{ row.current_value }}</span>
              </template>
            </el-table-column>
            <el-table-column label="剩余" width="132">
              <template #default="{ row }">{{ remainingText(row) }}</template>
            </el-table-column>
            <el-table-column label="到期日" width="150">
              <template #default="{ row }">
                <span v-if="row.due_at" class="tnum">{{ formatTime(row.due_at) }}</span>
                <span v-else class="muted">按需执行</span>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="92">
              <template #default="{ row }">
                <el-tag :type="statusMetaOf(row.status).type" size="small" effect="dark">
                  {{ statusMetaOf(row.status).label }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="依据" width="102">
              <template #default="{ row }">
                <el-button link type="primary" size="small" :icon="Document" @click="openEvidence(row)">
                  依据 ({{ row.evidence.length }})
                </el-button>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="132">
              <template #default="{ row }">
                <el-button
                  link
                  type="primary"
                  size="small"
                  :disabled="isClosed(row)"
                  @click="openAction(row, 'complete')"
                >
                  完成
                </el-button>
                <el-button
                  link
                  type="info"
                  size="small"
                  :disabled="isClosed(row)"
                  @click="openAction(row, 'skip')"
                >
                  跳过
                </el-button>
              </template>
            </el-table-column>

            <template #empty>
              <el-empty description="还没有计划，先生成一份" :image-size="80" />
            </template>
          </el-table>

          <el-pagination
            v-if="total > PAGE_SIZE"
            class="plans__pager"
            layout="prev, pager, next, total"
            size="small"
            :current-page="currentPage"
            :page-size="PAGE_SIZE"
            :total="total"
            @current-change="onPageChange"
          />
        </div>
      </section>
    </div>

    <!-- ----------------------------------------------------- ④ 依据抽屉 -->
    <el-drawer v-model="evidenceVisible" :title="evidenceTitle" size="540px">
      <div v-if="evidencePlan" class="evidence">
        <div class="evidence__head">
          <p class="evidence__name">{{ evidencePlan.item_name }}</p>
          <p class="evidence__meta">
            {{ evidencePlan.device_model }} / {{ evidencePlan.device_code }} ·
            {{ basisLabel(evidencePlan.cycle_basis) }} 周期
            {{ evidencePlan.cycle_value }}{{ basisUnit(evidencePlan.cycle_basis) }} ·
            当前 {{ evidencePlan.current_value }} ·
            剩余 {{ remainingText(evidencePlan) }}
          </p>
        </div>

        <div v-for="cite in evidencePlan.evidence" :key="cite.id" class="ev">
          <div class="ev__head">
            <span class="ev__doc">{{ cite.doc || '未标注文档' }}</span>
            <span v-if="cite.version" class="ev__chip">版本 {{ cite.version }}</span>
            <span v-if="cite.section" class="ev__chip">{{ cite.section }}</span>
            <span v-if="cite.page !== null" class="ev__chip">第 {{ cite.page }} 页</span>
            <span class="ev__chip text-mono">#{{ cite.id }}</span>
          </div>

          <img
            v-if="cite.image_url"
            class="ev__img"
            :src="cite.image_url"
            :alt="`引用 ${cite.id} 的原图`"
          />
          <p v-if="cite.snippet" class="ev__snippet">{{ cite.snippet }}</p>
          <p v-else class="ev__snippet muted">该引用没有正文片段，请到原文核对。</p>
        </div>

        <el-empty
          v-if="evidencePlan.evidence.length === 0"
          description="该项没有引用依据（evidence 为空，需人工确认）"
          :image-size="80"
        />
      </div>
    </el-drawer>

    <!-- ------------------------------------------------- ④ 完成 / 跳过弹窗 -->
    <el-dialog v-model="actionVisible" :title="actionTitle" width="460px">
      <p v-if="actionTarget" class="dialog__target">
        {{ actionTarget.item_name }} · {{ basisLabel(actionTarget.cycle_basis) }} 周期
        {{ actionTarget.cycle_value }}{{ basisUnit(actionTarget.cycle_basis) }} · 当前
        {{ actionTarget.current_value }}
      </p>

      <el-form label-width="88px" size="small" class="dialog__form">
        <el-form-item v-if="actionMode === 'complete'" label="完成值">
          <el-input-number
            v-model="actionForm.completed_value"
            :min="0"
            controls-position="right"
            class="dialog__num"
          />
          <p class="dialog__tip">
            填写完成时的计量值（如此刻的运行小时），后端据此滚动到下一周期；留空则用当前累计值。
          </p>
        </el-form-item>

        <el-form-item :label="actionMode === 'skip' ? '跳过原因' : '备注'">
          <el-input
            v-model="actionForm.note"
            type="textarea"
            :rows="3"
            :placeholder="
              actionMode === 'skip'
                ? '必填，例如：产线排产冲突，推迟到下周 PM 一并处理'
                : '选填，例如：更换了 O 型圈，顺带检查了真空度'
            "
          />
          <p v-if="actionMode === 'skip'" class="dialog__tip">
            跳过原因会写进计划记录；留空后端会记为「未填写原因」，等于把这次判断的依据丢掉。
          </p>
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button size="small" @click="actionVisible = false">取消</el-button>
        <el-button size="small" type="primary" :loading="actionSubmitting" @click="submitAction">
          {{ actionMode === 'skip' ? '确认跳过' : '确认完成' }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.plan {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  background: var(--surface-1);
}

/* 限宽居中：表格有十列，比管理页稍宽一点（1200 vs 1040），仍不至于在宽屏上散开 */
.plan__inner {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  width: 100%;
  max-width: 1200px;
  padding: var(--sp-6) var(--sp-5) var(--sp-10);
  margin: 0 auto;
}

.plan__head {
  margin-bottom: var(--sp-1);
}

.plan__title {
  margin: 0;
  font-size: var(--fs-xl);
  font-weight: 650;
  letter-spacing: -0.4px;
  color: var(--ink-900);
}

.plan__desc {
  margin: var(--sp-1) 0 0;
  font-size: var(--fs-sm);
  color: var(--ink-500);
}

/* ------------------------------------------------------------------ 面板 */
/* 与 AdminView 的 .panel 同款（scoped，各自持有一份）：半透明底 + 极淡描边，
   描边一旦明显，一列面板就会变成一堆框 */
.panel {
  background: rgba(22, 27, 34, 0.72);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(0, 212, 255, 0.12);
  border-radius: var(--r-lg);
  box-shadow: var(--sh-xs);
}

.panel__head {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-4) var(--sp-5);
  border-bottom: 1px solid var(--line-1);
}

.panel__head-text {
  min-width: 0;
}

.panel__title-row {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
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

.plan__count {
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
  color: var(--ink-400);
}

.hint--inline {
  margin: 0 0 var(--sp-3);
}

.muted {
  color: var(--ink-400);
}

/* ---------------------------------------------------------------- 生成表单 */
/* 两列排布，窄屏自动落成一列；字段数固定，不必写媒体查询 */
.gen-form__grid {
  display: grid;
  gap: 0 var(--sp-5);
  grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
}

.gen-form :deep(.el-form-item) {
  margin-bottom: var(--sp-4);
}

.gen-form__num {
  width: 100%;
}

/* 按钮区与字段之间加一条分隔线：一眼能分清「填完这里再点这里」 */
.gen-form__actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  padding-top: var(--sp-4);
  margin-top: var(--sp-1);
  border-top: 1px solid var(--line-2);
}

.plan__filters {
  display: flex;
  flex: 0 0 auto;
  flex-wrap: wrap;
  gap: var(--sp-2);
  align-items: center;
}

/* -------------------------------------------------------------- 未覆盖清单 */
.uncovered {
  margin-bottom: var(--sp-4);
  border-radius: var(--r-md);
}

.uncovered__list {
  padding-left: 18px;
  margin: 6px 0 0;
  font-size: var(--fs-sm);
  line-height: 1.75;
  color: var(--ink-700);
}

.uncovered__list li::marker {
  color: var(--warn-600);
}

/* ------------------------------------------------------------------ 表格 */
.preview {
  margin-top: var(--sp-1);
}

/* 已到期的行整体标红：这是整张表里最该被先看到的一行。
   左侧 3px 竖条负责在扫视时定位，底色负责把整行拉出来 */
.plans :deep(.plan-row--due) > td {
  background: var(--danger-50);
  color: var(--danger-600);
}

.plans :deep(.plan-row--due) > td:first-child {
  box-shadow: inset 3px 0 0 var(--danger-600);
}

.plans :deep(.plan-row--due:hover) > td {
  background: rgba(248, 113, 113, 0.16);
}

.plans__pager {
  justify-content: flex-end;
  margin-top: var(--sp-3);
}

/* ------------------------------------------------------------------ 抽屉 */
.evidence__head {
  padding-bottom: var(--sp-3);
  margin-bottom: var(--sp-4);
  border-bottom: 1px solid var(--line-1);
}

.evidence__name {
  margin: 0;
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--ink-900);
}

.evidence__meta {
  margin: 6px 0 0;
  font-size: var(--fs-xs);
  line-height: 1.7;
  color: var(--ink-500);
}

/* 每条引用一张卡：左侧 2px 青色竖条与引用卡片保持同一套视觉语言 */
.ev {
  padding: var(--sp-3) var(--sp-4);
  margin-bottom: var(--sp-3);
  background: var(--surface-2);
  border: 1px solid var(--line-1);
  border-left: 2px solid var(--brand-600);
  border-radius: var(--r-md);
}

.ev__head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: 6px;
}

.ev__doc {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--ink-900);
}

.ev__chip {
  padding: 1px 7px;
  font-size: 11px;
  color: var(--ink-500);
  background: var(--surface-0);
  border: 1px solid var(--line-1);
  border-radius: var(--r-pill);
}

.ev__snippet {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--ink-700);
  white-space: pre-wrap;
}

.ev__img {
  display: block;
  max-width: 100%;
  margin: 6px 0;
  border: 1px solid var(--line-1);
  border-radius: var(--r-sm);
}

/* ------------------------------------------------------------ 完成/跳过弹窗 */
.dialog__target {
  margin: 0 0 var(--sp-3);
  font-size: var(--fs-sm);
  color: var(--ink-700);
}

.dialog__form :deep(.el-form-item) {
  margin-bottom: var(--sp-3);
}

.dialog__num {
  width: 180px;
}

/* 提示文字占满一行：el-form-item__content 是 flex-wrap，给它 100% 基底即换行 */
.dialog__tip {
  flex: 1 0 100%;
  margin: 4px 0 0;
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--ink-400);
}

@media (max-width: 720px) {
  .plan__inner {
    padding: var(--sp-4) var(--sp-4) var(--sp-8);
  }

  .plan__filters {
    width: 100%;
  }
}
</style>
