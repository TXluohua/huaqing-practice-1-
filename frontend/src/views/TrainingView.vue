<script setup lang="ts">
/**
 * 考核认证页（功能③）。
 *
 * **独立性**：本页只依赖考核接口模块与 HTTP 基础设施，不 import 问答链路的任何模块
 * （聊天 API、SSE 工具、各 Pinia store 一律不碰），自己出题、自己交卷、自己发证。
 * 注意与问答页的「培训模式」（`mode=training` 的分层讲解）是两件事：那是问答能力，
 * 这里是独立考核功能，两者不共用状态、不互相跳转。
 *
 * 页内四条业务线，互不依赖（各自的 loading / 错误 / 空态都独立）：
 *   1. 出题区 —— `generateQuiz`；422 QUIZ_GENERATION_FAILED 是「抽不到有依据的题」，
 *      不是系统故障，给可执行的人话提示且不重试；
 *   2. 答题区 —— 逐题渲染 + 每题可展开「依据」；交卷时才把草稿按题目顺序转成提交格式；
 *   3. 判分结果区 —— 逐题对错 / 你的作答 / 标准答案 / 解析 / 依据；
 *      **交卷响应的 `certification_id` 恒为 null**（后端不自动发证），
 *      因此通过后只出现「申请认证」按钮，由用户显式指定等级与有效期后调 `issueCertification`；
 *   4. 认证记录区 —— 独立卡片，`listCertifications` 过滤 + 刷新；
 *      顶部另有 `listExpiringCertifications(30)` 的到期提醒（含已过期）。
 *
 * 证书口径：系统**不代表设备原厂签发**（`issuer` 固定「内部授权」，界面只写内部授权）。
 */
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'

import { formatTime, humanizeError, isApiError, type ApiError } from '@/api/http'
import {
  CERT_LEVELS,
  LEVEL_LABELS,
  TYPE_LABELS,
  certStatusMeta,
  generateQuiz,
  issueCertification,
  listCertifications,
  listExpiringCertifications,
  submitQuiz,
  toSubmitAnswer,
  type Certification,
  type CertificationRequest,
  type QuestionType,
  type Quiz,
  type QuizGenerateRequest,
  type QuizItem,
  type QuizResultItem,
  type QuizSubmitResponse,
} from '@/api/training'

// --------------------------------------------------------------------------- //
// 常量与展示辅助
// --------------------------------------------------------------------------- //

/** 一条依据（= 引用项）。类型从 QuizItem 的字段里取，省掉一次额外 import */
type Evidence = QuizItem['evidence'][number]

/** 难度选项：LEVEL_LABELS 是 Record，遍历对象取顺序不可控，这里固化成数组 */
const LEVEL_OPTIONS: { value: string; label: string }[] = Object.entries(LEVEL_LABELS).map(
  ([value, label]) => ({ value, label }),
)

/** 题型选项：顺序按「单选 / 多选 / 判断 / 简答」，默认只勾前两类的单选 + 判断 */
const TYPE_OPTIONS: { value: QuestionType; label: string }[] = (
  ['single', 'multiple', 'judgement', 'short'] as QuestionType[]
).map((value) => ({ value, label: TYPE_LABELS[value] }))

/** 选项字母：把「2」渲染成「C. 冷却水温度」，比光看下标好读 */
const OPTION_LETTERS = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'

/** 分数按百分比展示，最多一位小数（后端可能给 66.66666） */
function formatScore(value: number): string {
  return `${Math.round(value * 10) / 10}%`
}

/** 秒 → mm:ss（考核用时不会到小时级，不做 hh 分支） */
function formatDuration(totalSeconds: number): string {
  const safe = Math.max(0, Math.floor(totalSeconds))
  const minutes = Math.floor(safe / 60)
  const seconds = safe % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

/** 「Etcher-A 设备维护手册 · V2.1 · §4.1 真空度异常排查 · P.18」 */
function sourceLine(citation: Evidence): string {
  const parts = [
    citation.doc,
    citation.version ? `V${citation.version.replace(/^v/i, '')}` : null,
    citation.section ? `§${citation.section}` : null,
    citation.page ? `P.${citation.page}` : null,
  ]
  return parts.filter(Boolean).join(' · ') || '未标注出处'
}

/**
 * 作答值 → 人话。
 * `single` / `multiple` 在提交格式里是选项**下标**，要借原题的 options 还原成选项原文，
 * 否则结果区只会出现「2」这种没人看得懂的数字（简答的 expected 是要点数组）。
 */
function formatAnswer(value: unknown, type: string, options: string[]): string {
  if (value === undefined || value === null || value === '') return '未作答'

  if (type === 'judgement') {
    if (value === true || value === 'true') return '正确'
    if (value === false || value === 'false') return '错误'
    return String(value)
  }

  if (type === 'short') {
    return Array.isArray(value) ? value.map((point) => String(point)).join('；') : String(value)
  }

  const indexes = (Array.isArray(value) ? value : [value])
    .map((item) => Number(item))
    .filter((item) => Number.isInteger(item))
  if (!indexes.length) return '未作答'

  return indexes
    .map((index) => {
      const letter = OPTION_LETTERS[index] ?? String(index)
      const text = options[index]
      return text ? `${letter}. ${text}` : `选项 ${index}`
    })
    .join('，')
}

/**
 * 判分条目 → 人话作答文本。
 * 判分响应（`QuizResultItem`）只带 `no` / `question` / `expected` / `got`，**不带 type 与 options**，
 * 所以题型和选项原文都要用题号回查原卷（回查不到就按简答渲染，至少不会崩）。
 */
function answerText(item: QuizResultItem, which: 'expected' | 'got'): string {
  const origin = itemByNo.value.get(item.no)
  return formatAnswer(item[which], origin?.type ?? 'short', origin?.options ?? [])
}

/** 题号 → 题型中文（结果区展示用） */
function resultType(no: number): string {
  return itemByNo.value.get(no)?.type ?? 'short'
}

// --------------------------------------------------------------------------- //
// 1. 出题
// --------------------------------------------------------------------------- //

const quizForm = reactive({
  /** 默认 Etcher-A：演示数据里该型号的文档最全 */
  device_model: 'Etcher-A',
  /** 主题留空 = 不限主题（后端按设备型号全量检索）；填了就按主题缩小范围 */
  topic: '',
  level: 'basic',
  n_items: 5,
})

const selectedTypes = ref<QuestionType[]>(['single', 'judgement'])

const quizLoading = ref(false)
const quizError = ref<ApiError | null>(null)
const quiz = ref<Quiz | null>(null)

/** 试卷题目按 no 升序（提交顺序必须与它一致） */
const quizItems = computed<QuizItem[]>(() =>
  quiz.value ? [...quiz.value.items].sort((left, right) => left.no - right.no) : [],
)

/** 题号 → 原题：判分响应不带 type / options，结果区靠它回查题型与选项原文 */
const itemByNo = computed(() => new Map(quizItems.value.map((item) => [item.no, item])))

/**
 * 出题失败的人话文案。
 * 422 QUIZ_GENERATION_FAILED 的语义是「这个主题在知识库里抽不到有依据的句子」，
 * 属于**业务上正常的结果**（零幻觉策略：宁可不命题），不该当成系统故障，也不该重试。
 */
const quizErrorText = computed(() => {
  const error = quizError.value
  if (!error) return ''
  if (error.code === 'QUIZ_GENERATION_FAILED') {
    return `该主题抽不到有依据的题目，换个主题或设备再试。（${humanizeError(error)}）`
  }
  return humanizeError(error)
})

/** 离线出题提示：未配置文本模型时后端走确定性抽句，功能正常，不是错误 */
const offlineGenerator = computed(() => quiz.value?.generator === 'extractive-fallback')

/** 生成方式的说明文案：离线降级要说清「功能正常」，避免被当成失败 */
const generatorHint = computed(() =>
  offlineGenerator.value
    ? '未配置文本模型，后端走确定性抽句出题（原句 + 同片段其它数值做干扰项），功能正常'
    : '题目由文本模型生成，每题都经过依据校验',
)

async function onGenerate(): Promise<void> {
  if (!selectedTypes.value.length) {
    ElMessage.warning('请至少勾选一种题型。')
    return
  }

  const payload: QuizGenerateRequest = {
    device_model: quizForm.device_model.trim() || undefined,
    topic: quizForm.topic.trim() || undefined,
    level: quizForm.level,
    n_items: quizForm.n_items,
    question_types: [...selectedTypes.value],
  }

  quizLoading.value = true
  quizError.value = null
  try {
    const data = await generateQuiz(payload)
    // 换卷即清空上一轮的一切：草稿 / 判分 / 已发证书 / 计时
    quiz.value = data
    resetDrafts(data)
    result.value = null
    issuedCert.value = null
    trainee.value = trainee.value.trim()
    startTimer()
    // 依据校验会剔除不合规题目（零幻觉），缩水必须显性提示：
    // 只报实际题量的话，演示/验收方看不出是「本来就这么少」还是「被剔掉了」。
    const asked = data.requested_items ?? data.items.length
    const dropped = data.dropped_items ?? 0
    if (dropped > 0) {
      ElMessage.warning({
        message: `已生成 ${data.items.length} 道题（试卷 #${data.id}）：要求 ${asked} 题，${dropped} 题因依据不足已剔除`,
        duration: 6000,
        showClose: true,
      })
    } else {
      ElMessage.success(`已生成 ${data.items.length} 道题（试卷 #${data.id}）`)
    }
  } catch (error) {
    quiz.value = null
    drafts.value = {}
    result.value = null
    stopTimer()
    quizError.value = isApiError(error) ? error : null
    if (isApiError(error) && error.code === 'QUIZ_GENERATION_FAILED') {
      // 提示语同时给出「换个主题/设备」这个可执行动作与后端的具体原因
      ElMessage.warning({
        message: `该主题抽不到有依据的题目，换个主题或设备再试。（${humanizeError(error)}）`,
        duration: 6000,
        showClose: true,
      })
    } else {
      ElMessage.error(humanizeError(error))
    }
  } finally {
    quizLoading.value = false
  }
}

// --------------------------------------------------------------------------- //
// 2. 答题（内部草稿 → 提交格式）
// --------------------------------------------------------------------------- //

/**
 * 每题一份草稿。
 * 值统一按「好绑定的原始类型」存：选项下标存字符串（radio / checkbox 的取值），
 * 判断题存 'true' / 'false' 字符串，交卷时再由 `toSubmitAnswer` 转成接口要的数字 / 数组 / 布尔。
 */
interface DraftAnswer {
  single: string
  multiple: string[]
  judgement: string
  short: string
}

const drafts = ref<Record<number, DraftAnswer>>({})

function emptyDraft(): DraftAnswer {
  return { single: '', multiple: [], judgement: '', short: '' }
}

/** 生成 / 重做试卷时重建草稿；模板直接按下标取，靠这里保证 key 一定存在 */
function resetDrafts(data: Quiz): void {
  const next: Record<number, DraftAnswer> = {}
  for (const item of data.items) next[item.no] = emptyDraft()
  drafts.value = next
}

const trainee = ref('')
const elapsedSec = ref(0)
const result = ref<QuizSubmitResponse | null>(null)
const submitting = ref(false)

/** 计时起点用普通变量即可：它不参与渲染，只有 interval 回调读它 */
let startedAt: number | null = null
let tickTimer: number | undefined

/** 进入答题即开始计时（换卷 / 重新作答会重置），提交后停止 */
function startTimer(): void {
  stopTimer()
  startedAt = Date.now()
  elapsedSec.value = 0
  tickTimer = window.setInterval(() => {
    if (startedAt !== null) elapsedSec.value = Math.floor((Date.now() - startedAt) / 1000)
  }, 1000)
}

function stopTimer(): void {
  if (tickTimer !== undefined) {
    window.clearInterval(tickTimer)
    tickTimer = undefined
  }
}

function isAnswered(item: QuizItem): boolean {
  const draft = drafts.value[item.no]
  if (!draft) return false
  if (item.type === 'multiple') return draft.multiple.length > 0
  if (item.type === 'judgement') return draft.judgement !== ''
  if (item.type === 'short') return draft.short.trim() !== ''
  return draft.single !== ''
}

const answeredCount = computed(() => quizItems.value.filter((item) => isAnswered(item)).length)
const unansweredCount = computed(() => quizItems.value.length - answeredCount.value)

/**
 * 未作答的判断题数量。
 * `toSubmitAnswer('judgement', '')` 返回 `false`（判断只有真/假两态，没有「未作答」这个取值），
 * 所以漏选的判断题会以「错误」提交 —— 这必须在交卷前如实告知，不能让人以为漏选会被跳过。
 */
const unansweredJudgement = computed(
  () => quizItems.value.filter((item) => item.type === 'judgement' && !isAnswered(item)).length,
)

/** 草稿 → 提交值：判断/单选/多选的类型转换全部交给 api 层的 toSubmitAnswer */
function draftValue(item: QuizItem): unknown {
  const draft = drafts.value[item.no] ?? emptyDraft()
  switch (item.type) {
    case 'multiple':
      return draft.multiple
    case 'judgement':
      return draft.judgement
    case 'short':
      return draft.short
    default:
      return draft.single
  }
}

async function onSubmit(): Promise<void> {
  if (!quiz.value) return

  const traineeName = trainee.value.trim()
  if (!traineeName) {
    ElMessage.warning('请先填写答题人：证书上署的是这个名字。')
    return
  }

  if (unansweredCount.value > 0) {
    const judgementNote = unansweredJudgement.value
      ? `（其中 ${unansweredJudgement.value} 道判断题未选，会按「错误」提交）`
      : ''
    try {
      await ElMessageBox.confirm(
        `还有 ${unansweredCount.value} 题未作答${judgementNote}，确认现在交卷？`,
        '确认交卷',
        { confirmButtonText: '交卷', cancelButtonText: '继续答题', type: 'warning' },
      )
    } catch {
      return // 用户取消：留在答题状态，不打断已填内容
    }
  }

  // answers 的顺序 = 题目 no 的顺序（后端按下标判分，顺序错了就是另一份卷子）
  const answers = quizItems.value.map((item) => toSubmitAnswer(item.type, draftValue(item)))

  submitting.value = true
  try {
    const response = await submitQuiz(quiz.value.id, {
      trainee: traineeName,
      answers,
      duration_s: elapsedSec.value,
    })
    result.value = response
    issuedCert.value = null
    stopTimer()
    if (response.passed) ElMessage.success(`通过考核，得分 ${formatScore(response.score)}`)
    else ElMessage.warning(`未通过，得分 ${formatScore(response.score)}（及格线 ${formatScore(response.pass_line)}）`)
  } catch (error) {
    quizError.value = null
    ElMessage.error(humanizeError(error))
  } finally {
    submitting.value = false
  }
}

/** 重新作答：清掉判分结果与已发证书，草稿重置，计时重新开始 */
function onRetake(): void {
  if (!quiz.value) return
  resetDrafts(quiz.value)
  result.value = null
  issuedCert.value = null
  startTimer()
}

// --------------------------------------------------------------------------- //
// 3. 申请认证（发证必须由用户显式触发）
// --------------------------------------------------------------------------- //

const certDialogVisible = ref(false)
const issuing = ref(false)
/** 本次考核已签发的证书；非空表示不再重复发证 */
const issuedCert = ref<Certification | null>(null)

const certForm = reactive({
  level: CERT_LEVELS[0] as string,
  valid_days: 365,
  /** 固定「内部授权」：系统不代表设备原厂签发，不允许改成误导性的签发方 */
  issuer: '内部授权',
})

function openCertDialog(): void {
  if (!result.value?.passed) return
  certForm.level = CERT_LEVELS[0]
  certForm.valid_days = 365
  certDialogVisible.value = true
}

async function confirmIssue(): Promise<void> {
  if (!result.value || !quiz.value) return

  // 发证请求的 device_model 必填（后端 min_length=1）：出题时若留空过设备型号，这里要拦下来
  const deviceModel = quiz.value.device_model.trim()
  if (!deviceModel) {
    ElMessage.warning('这份试卷没有设备型号，无法发证：请在上方填写设备型号后重新出题。')
    return
  }

  const payload: CertificationRequest = {
    trainee: result.value.trainee,
    device_model: deviceModel,
    level: certForm.level,
    // attempt_id 来自交卷响应：发证绑定的是一次**通过**的考核（否则后端 400 ATTEMPT_NOT_PASSED）
    attempt_id: result.value.attempt_id,
    issuer: certForm.issuer,
    valid_days: certForm.valid_days,
  }

  issuing.value = true
  try {
    const cert = await issueCertification(payload)
    issuedCert.value = cert
    certDialogVisible.value = false
    ElMessage.success(`已签发证书 #${cert.id}，到期时间 ${formatTime(cert.expires_at)}`)
    await loadCertifications()
    await loadExpiring()
  } catch (error) {
    ElMessage.error(humanizeError(error))
  } finally {
    issuing.value = false
  }
}

// --------------------------------------------------------------------------- //
// 4. 认证记录（独立卡片，与答题互不依赖）
// --------------------------------------------------------------------------- //

const certQuery = reactive({ trainee: '' })
const certs = ref<Certification[]>([])
const certTotal = ref(0)
const certLoading = ref(false)

async function loadCertifications(): Promise<void> {
  certLoading.value = true
  try {
    const response = await listCertifications({
      trainee: certQuery.trainee.trim() || undefined,
      limit: 50,
    })
    certs.value = response.items
    certTotal.value = response.total
  } catch (error) {
    certs.value = []
    certTotal.value = 0
    ElMessage.error(humanizeError(error))
  } finally {
    certLoading.value = false
  }
}

// --------------------------------------------------------------------------- //
// 5. 到期提醒（含已过期）
// --------------------------------------------------------------------------- //

const EXPIRING_DAYS = 30

const expiringItems = ref<Certification[]>([])
const expiringTotal = ref(0)
const expiringExpanded = ref(false)

async function loadExpiring(): Promise<void> {
  try {
    const response = await listExpiringCertifications(EXPIRING_DAYS)
    expiringItems.value = response.items
    expiringTotal.value = response.total
  } catch (error) {
    // 提醒是辅助信息：失败不阻断主流程，但也如实提示（不静默吞掉）
    expiringItems.value = []
    expiringTotal.value = 0
    ElMessage.error(humanizeError(error))
  }
}

onMounted(() => {
  void loadCertifications()
  void loadExpiring()
})

// 离开页面必须停掉计时器，否则组件已卸载而 interval 还在跑
onBeforeUnmount(stopTimer)
</script>

<template>
  <div class="training">
    <div class="training__inner">
      <header class="training__head">
        <h1 class="training__title">考核认证</h1>
        <p class="training__desc">
          基于知识库依据出题、确定性判分、按需发证。题目与依据全部来自检索到的文档片段，
          抽不到有依据的题就不出题；系统不代表设备原厂签发证书。
        </p>
      </header>

      <!-- ------------------------------------------------------------ 到期提醒 -->
      <el-alert
        v-if="expiringTotal > 0"
        class="expiring"
        type="warning"
        :closable="false"
        show-icon
      >
        <template #title>
          <span class="expiring__toggle" @click="expiringExpanded = !expiringExpanded">
            {{ EXPIRING_DAYS }} 天内到期或已过期共 {{ expiringTotal }} 条
            <span class="expiring__caret">{{ expiringExpanded ? '收起' : '展开' }}</span>
          </span>
        </template>

        <ul v-if="expiringExpanded" class="expiring__list">
          <li v-for="cert in expiringItems" :key="cert.id" class="expiring__item">
            <span class="expiring__name">{{ cert.trainee }}</span>
            <span class="expiring__meta">{{ cert.device_model }} · {{ cert.level }}</span>
            <span class="expiring__meta tnum">到期 {{ formatTime(cert.expires_at) }}</span>
            <el-tag :type="certStatusMeta(cert).type" size="small" effect="plain">
              {{ certStatusMeta(cert).label }}
            </el-tag>
          </li>
          <li v-if="!expiringItems.length" class="expiring__item expiring__item--empty">
            后端未返回明细
          </li>
        </ul>
      </el-alert>

      <!-- ---------------------------------------------------------------- 出题 -->
      <el-card class="card" shadow="never">
        <template #header>
          <div class="card__head">
            <div class="card__head-text">
              <span class="card__title">出题</span>
              <span class="card__desc">先检索知识库片段，再按片段出题；每题都带可展开的依据</span>
            </div>
            <!-- 生成方式（generator）：离线降级只是中性提示，不是错误 -->
            <el-tooltip v-if="quiz" :content="generatorHint" placement="top">
              <el-tag size="small" type="info" effect="plain">
                {{ offlineGenerator ? '离线出题（未配置文本模型）' : quiz.generator }}
              </el-tag>
            </el-tooltip>
          </div>
        </template>

        <el-form label-width="88px" size="small" class="form">
          <div class="form__grid">
            <el-form-item label="设备型号">
              <el-input v-model="quizForm.device_model" placeholder="如：Etcher-A" />
            </el-form-item>
            <el-form-item label="主题">
              <el-input v-model="quizForm.topic" placeholder="如：真空系统（留空 = 不限主题）" clearable />
            </el-form-item>
            <el-form-item label="难度">
              <el-radio-group v-model="quizForm.level">
                <el-radio-button
                  v-for="option in LEVEL_OPTIONS"
                  :key="option.value"
                  :value="option.value"
                >
                  {{ option.label }}
                </el-radio-button>
              </el-radio-group>
            </el-form-item>
            <el-form-item label="题目数量">
              <el-input-number v-model="quizForm.n_items" :min="1" :max="10" controls-position="right" />
            </el-form-item>
          </div>

          <el-form-item label="题型">
            <el-checkbox-group v-model="selectedTypes" class="types">
              <el-checkbox v-for="option in TYPE_OPTIONS" :key="option.value" :value="option.value">
                {{ option.label }}
              </el-checkbox>
            </el-checkbox-group>
          </el-form-item>

          <el-form-item>
            <el-button type="primary" :loading="quizLoading" @click="onGenerate">生成题目</el-button>
            <span v-if="quiz" class="quiz-meta">
              试卷 #{{ quiz.id }} · {{ quiz.device_model }} ·
              {{ quiz.topic || '不限主题' }} · {{ LEVEL_LABELS[quiz.level] ?? quiz.level }} ·
              {{ quiz.n_items }} 题 · {{ formatTime(quiz.created_at) }}
            </span>
          </el-form-item>
        </el-form>

        <!-- 出题失败：422 是「抽不到有依据的题」，不是系统故障，配色上用 warn 而不是 error -->
        <div
          v-if="quizError"
          class="note"
          :class="quizError.code === 'QUIZ_GENERATION_FAILED' ? 'note--warn' : 'note--error'"
        >
          <p class="note__title">出题失败：{{ quizError.code }}</p>
          <p class="note__text">{{ quizErrorText }}</p>
        </div>
      </el-card>

      <!-- ---------------------------------------------------------------- 答题 -->
      <el-card v-if="quiz" class="card" shadow="never">
        <template #header>
          <div class="card__head">
            <div class="card__head-text">
              <span class="card__title">答题</span>
              <span class="card__desc">
                共 {{ quizItems.length }} 题 · 已作答 {{ answeredCount }}/{{ quizItems.length }}
              </span>
            </div>
            <div class="answer-head">
              <el-input
                v-model="trainee"
                size="small"
                class="answer-head__field"
                placeholder="答题人（必填）"
                :disabled="!!result"
              />
              <span class="timer tnum" :title="'进入答题后的用时，交卷时作为 duration_s 上报'">
                {{ formatDuration(elapsedSec) }}
              </span>
            </div>
          </div>
        </template>

        <el-empty v-if="!quizItems.length" description="这份试卷没有题目，请重新出题" />

        <div v-else class="questions">
          <article v-for="item in quizItems" :key="item.no" class="q">
            <div class="q__head">
              <span class="q__no tnum">第 {{ item.no }} 题</span>
              <span class="q__type">{{ TYPE_LABELS[item.type] ?? item.type }}</span>
            </div>

            <p class="q__text">{{ item.question }}</p>

            <!-- 单选：选项下标（字符串形式） -->
            <el-radio-group
              v-if="item.type === 'single'"
              v-model="drafts[item.no].single"
              class="q__options"
              :disabled="!!result"
            >
              <el-radio v-for="(option, index) in item.options" :key="index" :value="String(index)">
                <span class="opt__idx">{{ OPTION_LETTERS[index] }}</span>{{ option }}
              </el-radio>
            </el-radio-group>

            <!-- 多选：下标数组 -->
            <el-checkbox-group
              v-else-if="item.type === 'multiple'"
              v-model="drafts[item.no].multiple"
              class="q__options"
              :disabled="!!result"
            >
              <el-checkbox
                v-for="(option, index) in item.options"
                :key="index"
                :value="String(index)"
              >
                <span class="opt__idx">{{ OPTION_LETTERS[index] }}</span>{{ option }}
              </el-checkbox>
            </el-checkbox-group>

            <!-- 判断：内部存 'true' / 'false'，交卷时由 toSubmitAnswer 转成布尔 -->
            <el-radio-group
              v-else-if="item.type === 'judgement'"
              v-model="drafts[item.no].judgement"
              class="q__options"
              :disabled="!!result"
            >
              <el-radio value="true">正确</el-radio>
              <el-radio value="false">错误</el-radio>
            </el-radio-group>

            <!-- 简答：要点覆盖判分，提示写清「按要点答」 -->
            <el-input
              v-else
              v-model="drafts[item.no].short"
              type="textarea"
              :rows="3"
              :disabled="!!result"
              placeholder="按要点作答，写出关键数值与步骤"
            />

            <!-- 每题的「依据」必须可展开：没有依据的题后端不会出 -->
            <el-collapse class="evidence">
              <el-collapse-item :name="`evidence-${item.no}`">
                <template #title>
                  <span class="evidence__title">依据（{{ item.evidence.length }} 条）</span>
                </template>

                <ul class="evidence__list">
                  <li
                    v-for="(citation, index) in item.evidence"
                    :key="index"
                    class="evidence__item"
                  >
                    <div class="evidence__head">
                      <span class="evidence__idx tnum">{{ index + 1 }}</span>
                      <span class="evidence__source">{{ sourceLine(citation) }}</span>
                    </div>
                    <p v-if="citation.snippet" class="evidence__snippet">{{ citation.snippet }}</p>
                    <p v-else class="evidence__snippet evidence__snippet--empty">
                      （后端未返回原文片段）
                    </p>
                  </li>
                </ul>
              </el-collapse-item>
            </el-collapse>
          </article>
        </div>

        <div class="submit-bar">
          <span v-if="result" class="submit-bar__note">已交卷，如需重做请点「重新作答」</span>
          <span v-else-if="unansweredCount" class="submit-bar__note submit-bar__note--warn">
            还有 {{ unansweredCount }} 题未作答
          </span>
          <span v-else class="submit-bar__note">全部题目已作答</span>

          <el-button
            type="primary"
            size="small"
            :loading="submitting"
            :disabled="!!result || !quizItems.length"
            @click="onSubmit"
          >
            交卷判分
          </el-button>
        </div>
      </el-card>

      <!-- ------------------------------------------------------------ 判分结果 -->
      <el-card v-if="result" class="card" shadow="never">
        <template #header>
          <div class="card__head">
            <div class="card__head-text">
              <span class="card__title">判分结果</span>
              <span class="card__desc">
                考核 #{{ result.attempt_id }} · {{ result.trainee }} ·
                用时 {{ formatDuration(elapsedSec) }}
              </span>
            </div>
            <el-button size="small" @click="onRetake">重新作答</el-button>
          </div>
        </template>

        <el-result
          :icon="result.passed ? 'success' : 'warning'"
          :title="result.passed ? '通过考核' : '未通过'"
          :sub-title="`得分 ${formatScore(result.score)} · 及格线 ${formatScore(result.pass_line)}`"
        >
          <template #extra>
            <!--
              关键边界：交卷响应里的 certification_id 恒为 null（后端不自动发证），
              所以这里**只**给「申请认证」入口，通过后由用户显式指定等级与有效期。
            -->
            <el-button
              v-if="result.passed"
              type="primary"
              :disabled="!!issuedCert"
              @click="openCertDialog"
            >
              {{ issuedCert ? `已发证 #${issuedCert.id}` : '申请认证' }}
            </el-button>
            <el-tooltip v-else content="需先通过一次考核" placement="top">
              <span class="apply-disabled">
                <el-button type="primary" disabled>申请认证</el-button>
              </span>
            </el-tooltip>
          </template>
        </el-result>

        <el-alert
          v-if="issuedCert"
          class="issued"
          type="success"
          :closable="false"
          show-icon
          :title="`已签发证书 #${issuedCert.id}（${issuedCert.level}，${formatTime(issuedCert.issued_at)} 至 ${formatTime(issuedCert.expires_at)}）`"
        >
          <p class="issued__note">{{ issuedCert.note }}</p>
        </el-alert>

        <el-empty v-if="!result.detail.length" description="后端未返回逐题判分明细" />

        <div v-else class="details">
          <article
            v-for="item in result.detail"
            :key="item.no"
            class="detail"
            :class="item.correct ? 'detail--ok' : 'detail--bad'"
          >
            <div class="detail__head">
              <span class="detail__no tnum">第 {{ item.no }} 题</span>
              <span class="q__type">{{ TYPE_LABELS[resultType(item.no)] ?? resultType(item.no) }}</span>
              <el-tag :type="item.correct ? 'success' : 'danger'" size="small" effect="plain">
                {{ item.correct ? '正确' : '错误' }}
              </el-tag>
            </div>

            <p class="detail__question">{{ item.question }}</p>

            <dl class="detail__answers">
              <div class="detail__pair">
                <dt>你的作答</dt>
                <dd :class="{ 'detail__value--miss': answerText(item, 'got') === '未作答' }">
                  {{ answerText(item, 'got') }}
                </dd>
              </div>
              <div class="detail__pair">
                <dt>标准答案</dt>
                <dd>{{ answerText(item, 'expected') }}</dd>
              </div>
            </dl>

            <p v-if="item.explanation" class="detail__explain">解析：{{ item.explanation }}</p>

            <el-collapse v-if="item.evidence.length" class="evidence">
              <el-collapse-item :name="`result-evidence-${item.no}`">
                <template #title>
                  <span class="evidence__title">依据（{{ item.evidence.length }} 条）</span>
                </template>

                <ul class="evidence__list">
                  <li
                    v-for="(citation, index) in item.evidence"
                    :key="index"
                    class="evidence__item"
                  >
                    <div class="evidence__head">
                      <span class="evidence__idx tnum">{{ index + 1 }}</span>
                      <span class="evidence__source">{{ sourceLine(citation) }}</span>
                    </div>
                    <p v-if="citation.snippet" class="evidence__snippet">{{ citation.snippet }}</p>
                    <p v-else class="evidence__snippet evidence__snippet--empty">
                      （后端未返回原文片段）
                    </p>
                  </li>
                </ul>
              </el-collapse-item>
            </el-collapse>
          </article>
        </div>
      </el-card>

      <!-- ------------------------------------------------------------ 认证记录 -->
      <el-card class="card" shadow="never">
        <template #header>
          <div class="card__head">
            <div class="card__head-text">
              <span class="card__title">认证记录</span>
              <span class="card__desc">
                签发方为内部授权，备注由后端固定标注，界面上不做截断
              </span>
            </div>
            <el-button size="small" :icon="Refresh" :loading="certLoading" @click="loadCertifications">
              刷新
            </el-button>
          </div>
        </template>

        <div class="cert-toolbar">
          <el-input
            v-model="certQuery.trainee"
            size="small"
            class="cert-toolbar__field"
            placeholder="按答题人过滤，留空看全部"
            clearable
            @keyup.enter="loadCertifications"
            @clear="loadCertifications"
          />
          <el-button size="small" type="primary" @click="loadCertifications">查询</el-button>
          <span class="cert-toolbar__total tnum">共 {{ certTotal }} 张</span>
        </div>

        <el-table :data="certs" size="small" v-loading="certLoading" class="certs">
          <el-table-column prop="id" label="证书 ID" width="88" />
          <el-table-column prop="trainee" label="姓名" width="100" />
          <el-table-column prop="device_model" label="设备" width="110" show-overflow-tooltip />
          <el-table-column prop="level" label="等级" width="70" />
          <el-table-column label="分数" width="80">
            <template #default="{ row }">
              <span class="tnum">{{ formatScore(row.score) }}</span>
            </template>
          </el-table-column>
          <el-table-column prop="issuer" label="签发方" width="100" />
          <el-table-column label="签发时间" width="150">
            <template #default="{ row }">
              <span class="tnum">{{ formatTime(row.issued_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="到期时间" width="150">
            <template #default="{ row }">
              <span class="tnum">{{ formatTime(row.expires_at) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="130">
            <template #default="{ row }">
              <el-tag :type="certStatusMeta(row).type" size="small" effect="plain">
                {{ certStatusMeta(row).label }}
              </el-tag>
            </template>
          </el-table-column>
          <!-- 备注是后端固定标注的免责说明，整句展示，不做省略 -->
          <el-table-column label="备注" min-width="280">
            <template #default="{ row }">
              <span class="cert-note">{{ row.note }}</span>
            </template>
          </el-table-column>

          <template #empty>
            <el-empty description="暂无认证记录" :image-size="80" />
          </template>
        </el-table>
      </el-card>
    </div>

    <!-- ------------------------------------------------------------ 发证对话框 -->
    <el-dialog
      v-model="certDialogVisible"
      title="申请认证"
      width="460px"
      :close-on-click-modal="false"
    >
      <el-form v-if="result" label-width="96px" size="small">
        <el-form-item label="答题人">
          <span>{{ result.trainee }}</span>
        </el-form-item>
        <el-form-item label="设备型号">
          <span>{{ quiz?.device_model || '（未指定，无法发证）' }}</span>
        </el-form-item>
        <el-form-item label="考核成绩">
          <span class="tnum">
            {{ formatScore(result.score) }}（及格线 {{ formatScore(result.pass_line) }}）
          </span>
        </el-form-item>
        <el-form-item label="考核记录">
          <span class="text-mono">attempt_id = {{ result.attempt_id }}</span>
        </el-form-item>
        <el-form-item label="等级">
          <el-radio-group v-model="certForm.level">
            <el-radio-button v-for="level in CERT_LEVELS" :key="level" :value="level">
              {{ level }}
            </el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="有效期">
          <el-input-number v-model="certForm.valid_days" :min="1" :max="3650" controls-position="right" />
          <span class="hint hint--inline">天</span>
        </el-form-item>
        <el-form-item label="签发方">
          <el-input v-model="certForm.issuer" disabled />
        </el-form-item>
        <p class="hint">
          系统不代表设备原厂签发，签发方固定为「内部授权」，证书备注会注明该口径。
        </p>
      </el-form>

      <template #footer>
        <el-button size="small" @click="certDialogVisible = false">取消</el-button>
        <el-button size="small" type="primary" :loading="issuing" @click="confirmIssue">
          确认发证
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.training {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  background: var(--surface-1);
}

/* 限宽居中：本页表单 + 表格混排，铺满宽屏后 label 与输入框会离得很远 */
.training__inner {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  width: 100%;
  max-width: 1080px;
  padding: var(--sp-6) var(--sp-5) var(--sp-10);
  margin: 0 auto;
}

.training__head {
  margin-bottom: var(--sp-1);
}

.training__title {
  margin: 0;
  font-size: var(--fs-xl);
  font-weight: 650;
  letter-spacing: -0.4px;
  color: var(--ink-900);
}

.training__desc {
  max-width: 760px;
  margin: var(--sp-1) 0 0;
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--ink-500);
}

/* ---------------------------------------------------------------- 到期提醒 */
.expiring {
  border-radius: var(--r-md);
}

.expiring__toggle {
  cursor: pointer;
  user-select: none;
}

.expiring__caret {
  margin-left: var(--sp-2);
  font-size: var(--fs-xs);
  color: var(--brand-600);
}

.expiring__list {
  padding: 0;
  margin: var(--sp-3) 0 0;
  list-style: none;
}

/* 每条一行：姓名 / 设备·等级 / 到期时间 / 状态标签，靠 gap 对齐成列 */
.expiring__item {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-3);
  padding: 5px 0;
  font-size: var(--fs-xs);
  border-top: 1px solid rgba(251, 191, 36, 0.18);
}

.expiring__item--empty {
  color: var(--ink-400);
}

.expiring__name {
  min-width: 72px;
  font-weight: 600;
  color: var(--ink-900);
}

.expiring__meta {
  color: var(--ink-500);
}

/* ------------------------------------------------------------------ 卡片 */
/* el-card 只当容器用，标题层级由 .card__head 自绘：EP 默认头部内边距偏大，
   一页摞四张卡会显得很「后台系统」 */
.card {
  background: rgba(22, 27, 34, 0.72);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(0, 212, 255, 0.12);
  border-radius: var(--r-lg);
  box-shadow: var(--sh-sm);
}

.card :deep(.el-card__header) {
  padding: var(--sp-3) var(--sp-5);
  border-bottom: 1px solid var(--line-1);
}

.card :deep(.el-card__body) {
  padding: var(--sp-5);
}

.card__head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-3);
}

.card__head-text {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.card__title {
  font-size: var(--fs-base);
  font-weight: 600;
  color: var(--ink-900);
}

.card__desc {
  margin-top: 2px;
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--ink-500);
}

/* -------------------------------------------------------------------- 表单 */
.form__grid {
  display: grid;
  gap: 0 var(--sp-5);
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
}

.form :deep(.el-form-item) {
  margin-bottom: var(--sp-4);
}

.types {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2) var(--sp-5);
}

.quiz-meta {
  margin-left: var(--sp-3);
  font-size: var(--fs-xs);
  color: var(--ink-400);
}

/* ------------------------------------------------------------------ 提示条 */
.note {
  padding: 10px var(--sp-4);
  border: 1px solid;
  border-radius: var(--r-md);
}

.note--warn {
  background: var(--warn-50);
  border-color: rgba(251, 191, 36, 0.3);
}

.note--error {
  background: var(--danger-50);
  border-color: rgba(248, 113, 113, 0.32);
}

.note__title {
  margin: 0 0 3px;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--warn-600);
}

.note--error .note__title {
  color: var(--danger-600);
}

.note__text {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--ink-500);
}

.hint {
  margin: 6px 0 0;
  font-size: var(--fs-xs);
  color: var(--ink-400);
}

.hint--inline {
  margin: 0 0 0 var(--sp-2);
}

/* -------------------------------------------------------------------- 答题 */
.answer-head {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: var(--sp-3);
}

.answer-head__field {
  width: 180px;
}

/* 计时用等宽数位 + 青色：它是本页唯一「在动」的数字，一眼能找到 */
.timer {
  min-width: 62px;
  font-size: var(--fs-base);
  font-weight: 600;
  color: var(--brand-400);
  text-align: right;
  letter-spacing: 0.5px;
}

.questions {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
}

.q {
  padding: var(--sp-4) var(--sp-4) var(--sp-3);
  background: var(--surface-0);
  border: 1px solid var(--line-1);
  border-radius: var(--r-md);
}

.q__head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: 6px;
}

.q__no {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--brand-400);
}

/* 题型标签与题号同一行，用中性 pill：题型只是分类，不需要抢注意力 */
.q__type {
  padding: 1px 8px;
  font-size: 11px;
  color: var(--ink-500);
  background: var(--surface-2);
  border-radius: var(--r-pill);
}

.q__text {
  margin: 0 0 var(--sp-3);
  font-size: var(--fs-base);
  line-height: 1.75;
  color: var(--ink-900);
  word-break: break-word;
}

/* 选项竖排：横排时长选项会挤成一团，现场读题容易看错行 */
.q__options {
  display: flex;
  flex-direction: column;
  gap: 4px;
  align-items: flex-start;
}

/* EP 的 radio / checkbox 默认 nowrap + 固定行高，长选项会被截断 */
.q__options :deep(.el-radio),
.q__options :deep(.el-checkbox) {
  display: flex;
  align-items: flex-start;
  height: auto;
  margin-right: 0;
  white-space: normal;
}

.q__options :deep(.el-radio__label),
.q__options :deep(.el-checkbox__label) {
  line-height: 1.7;
  white-space: normal;
  word-break: break-word;
}

.opt__idx {
  display: inline-block;
  min-width: 16px;
  margin-right: 4px;
  font-weight: 600;
  color: var(--brand-400);
}

/* ------------------------------------------------------------------ 依据 */
/* 依据折叠面板：默认收起（否则一页被原文片段淹掉），但每题都必须在，
   因为「没有依据的题后端不会出」是本功能的核心承诺 */
.evidence {
  margin-top: var(--sp-3);
  border-top: 1px solid var(--line-2);
  border-bottom: none;
}

.evidence :deep(.el-collapse-item__header) {
  height: 34px;
  font-size: var(--fs-xs);
  line-height: 34px;
  color: var(--ink-500);
  background: transparent;
  border-bottom: none;
}

.evidence :deep(.el-collapse-item__wrap) {
  background: transparent;
  border-bottom: none;
}

.evidence :deep(.el-collapse-item__content) {
  padding-bottom: var(--sp-2);
}

.evidence__title {
  color: var(--brand-600);
}

.evidence__list {
  padding: 0;
  margin: 0;
  list-style: none;
}

.evidence__item {
  padding: var(--sp-2) var(--sp-3);
  margin-bottom: var(--sp-2);
  background: var(--surface-2);
  border-left: 3px solid var(--brand-200);
  border-radius: 0 var(--r-sm) var(--r-sm) 0;
}

.evidence__head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

.evidence__idx {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  min-width: 17px;
  height: 17px;
  font-size: 10px;
  font-weight: 700;
  color: #04121a;
  background: var(--brand-600);
  border-radius: 4px;
}

.evidence__source {
  font-size: var(--fs-xs);
  color: var(--ink-500);
  word-break: break-word;
}

.evidence__snippet {
  margin: 5px 0 0;
  font-size: var(--fs-xs);
  line-height: 1.7;
  color: var(--ink-700);
  word-break: break-word;
  white-space: pre-wrap;
}

.evidence__snippet--empty {
  color: var(--ink-400);
}

/* ---------------------------------------------------------------- 交卷条 */
.submit-bar {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: var(--sp-3);
  padding-top: var(--sp-4);
  margin-top: var(--sp-4);
  border-top: 1px solid var(--line-1);
}

.submit-bar__note {
  font-size: var(--fs-xs);
  color: var(--ink-500);
}

.submit-bar__note--warn {
  color: var(--warn-600);
}

/* ---------------------------------------------------------------- 判分结果 */
.apply-disabled {
  display: inline-block;
}

.issued {
  margin-bottom: var(--sp-4);
  border-radius: var(--r-md);
}

.issued__note {
  margin: 0;
  font-size: var(--fs-xs);
  line-height: 1.7;
}

.details {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
}

/* 左侧一条对错色竖条：十几道题的明细扫下来，红绿比文字先到眼睛 */
.detail {
  padding: var(--sp-4);
  background: var(--surface-0);
  border: 1px solid var(--line-1);
  border-left: 3px solid var(--ink-300);
  border-radius: var(--r-md);
}

.detail--ok {
  border-left-color: var(--ok-600);
}

.detail--bad {
  border-left-color: var(--danger-600);
}

.detail__head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}

.detail__no {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--ink-900);
}

.detail__question {
  margin: var(--sp-2) 0 var(--sp-3);
  font-size: var(--fs-sm);
  line-height: 1.75;
  color: var(--ink-700);
  word-break: break-word;
}

.detail__answers {
  display: grid;
  gap: var(--sp-2) var(--sp-4);
  margin: 0;
  grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
}

.detail__pair {
  padding: 7px var(--sp-3);
  background: var(--surface-2);
  border-radius: var(--r-sm);
}

.detail__pair dt {
  font-size: 11px;
  color: var(--ink-400);
}

.detail__pair dd {
  margin: 2px 0 0;
  font-size: var(--fs-sm);
  line-height: 1.7;
  color: var(--ink-900);
  word-break: break-word;
}

.detail__value--miss {
  color: var(--ink-400);
}

.detail__explain {
  margin: var(--sp-3) 0 0;
  font-size: var(--fs-xs);
  line-height: 1.7;
  color: var(--ink-500);
  word-break: break-word;
}

/* ---------------------------------------------------------------- 认证记录 */
.cert-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-4);
}

.cert-toolbar__field {
  max-width: 240px;
}

.cert-toolbar__total {
  margin-left: auto;
  font-size: var(--fs-xs);
  color: var(--ink-400);
}

.certs {
  width: 100%;
}

/* 备注是免责说明，允许换行整句展示；不设省略号、不做 line-clamp */
.cert-note {
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--ink-500);
  word-break: break-word;
  white-space: normal;
}

.certs :deep(.el-table__empty-block) {
  background: transparent;
}

@media (max-width: 720px) {
  .training__inner {
    padding-right: var(--sp-4);
    padding-left: var(--sp-4);
  }

  .answer-head {
    width: 100%;
  }

  .answer-head__field {
    flex: 1;
    width: auto;
  }
}
</style>
