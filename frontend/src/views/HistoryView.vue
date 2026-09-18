<script setup lang="ts">
/**
 * 历史会话页（FR-06）。
 *
 * 布局：左侧会话列表（关键字搜索 + 分页），右侧该会话的消息回看。
 * 选中会话后可以「继续对话」——把会话号交给 chat store 再跳回问答页，
 * 这样多轮上下文由后端的检查点（thread_id）续上，前端不需要自己拼 history。
 *
 * 本页是**唯一能提交反馈的地方**：SSE 的 done 事件不带 qa_id，
 * 只有历史消息（qa_record 表）才带（接口文档 §4.5、§8 缺口）。
 *
 * 助手的回答走与问答页同一套 markdown 渲染（utils/markdown.ts），
 * 因此历史里的 [n] 角标同样可点。用户提问保持 pre-wrap 纯文本。
 */
import { Search } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { listMessages, type Citation, type MessageItem } from '@/api/chat'
import { formatTime, humanizeError, isApiError, type ApiError } from '@/api/http'
import CitationCard from '@/components/citation/CitationCard.vue'
import SourceDrawer from '@/components/citation/SourceDrawer.vue'
import ConfidenceTag from '@/components/common/ConfidenceTag.vue'
import FeedbackBar from '@/components/common/FeedbackBar.vue'
import StatusBadge from '@/components/common/StatusBadge.vue'
import { useChatStore } from '@/stores/chat'
import { useSessionStore } from '@/stores/session'
import { citationIdFromEvent, renderMarkdown } from '@/utils/markdown'

const sessionStore = useSessionStore()
const chatStore = useChatStore()
const router = useRouter()

const keywordInput = ref('')
const activeSessionId = ref<string | null>(null)
const detailMessages = ref<MessageItem[]>([])
const detailLoading = ref(false)
const detailError = ref<ApiError | null>(null)

const drawerVisible = ref(false)
const activeCitation = ref<Citation | null>(null)

/** 按 qa_id + role 生成稳定 key，与后端「一轮 = 两条记录」的结构一致 */
const detailTitle = computed(
  () => sessionStore.sessions.find((item) => item.session_id === activeSessionId.value)?.title ?? '',
)

/**
 * 会话行的 LED 色调。
 *
 * 后端的 SessionItem 没有状态字段（接口文档 §4.4 只有标题/条数/时间），
 * 所以这里**不臆造状态**，只用 message_count 推断「这轮问答是否落齐了」：
 * 助手回答写库失败时该会话就会停在奇数条上。这是纯展示用的保守推断，
 * 不触发任何额外请求；真要区分「进行中 / 出错」，得先让后端在列表里带上状态。
 */
function sessionTone(item: { message_count: number }): string {
  if (item.message_count >= 2) return 'ok'
  return 'warn' // 0 条（空会话）或 1 条（只有提问，回答未落库）
}

onMounted(async () => {
  try {
    await sessionStore.fetchSessions({ resetOffset: true })
  } catch (error) {
    ElMessage.error(humanizeError(error))
  }
})

async function onSearch(): Promise<void> {
  try {
    activeSessionId.value = null
    detailMessages.value = []
    await sessionStore.search(keywordInput.value)
  } catch (error) {
    ElMessage.error(humanizeError(error))
  }
}

async function onPageChange(target: number): Promise<void> {
  try {
    await sessionStore.goToPage(target)
  } catch (error) {
    ElMessage.error(humanizeError(error))
  }
}

async function openSession(sessionId: string): Promise<void> {
  activeSessionId.value = sessionId
  detailLoading.value = true
  detailError.value = null

  try {
    const response = await listMessages(sessionId, { with_citations: true, limit: 200 })
    detailMessages.value = response.items
    // 顺手把会话标为当前会话，用户点「继续对话」时不用再选一次
    sessionStore.setCurrent(sessionId, detailTitle.value)
  } catch (error) {
    detailMessages.value = []
    detailError.value = isApiError(error) ? error : null
  } finally {
    detailLoading.value = false
  }
}

async function continueChat(): Promise<void> {
  if (!activeSessionId.value) return
  try {
    await chatStore.loadHistory(activeSessionId.value)
    await router.push('/chat')
  } catch (error) {
    ElMessage.error(humanizeError(error))
  }
}

function openCitation(citation: Citation): void {
  activeCitation.value = citation
  drawerVisible.value = true
}

/** 点正文里的 [n]：在历史记录的引用清单里按 id 找（找不到就忽略，不报错） */
function onContentClick(event: MouseEvent, citations: Citation[] | undefined): void {
  const id = citationIdFromEvent(event)
  if (id === null) return
  const citation = citations?.find((item) => item.id === id)
  if (citation) openCitation(citation)
}
</script>

<template>
  <div class="history">
    <!-- 左：会话列表 -->
    <aside class="history__side">
      <div class="history__side-head">
        <span class="history__side-title">历史会话</span>
        <span v-if="sessionStore.total" class="history__count tnum">{{ sessionStore.total }}</span>
      </div>

      <div class="history__search">
        <el-input
          v-model="keywordInput"
          size="small"
          placeholder="按标题搜索"
          :prefix-icon="Search"
          clearable
          @keyup.enter="onSearch"
          @clear="onSearch"
        />
        <el-button size="small" type="primary" @click="onSearch">搜索</el-button>
      </div>

      <!-- 列表加载失败时给明确错误态，而不是渲染空列表 -->
      <div v-if="sessionStore.error" class="history__side-body">
        <el-result
          icon="warning"
          title="会话列表加载失败"
          :sub-title="`${sessionStore.error.code}：${humanizeError(sessionStore.error)}`"
        >
          <template #extra>
            <el-button size="small" type="primary" @click="sessionStore.fetchSessions()">
              重试
            </el-button>
          </template>
        </el-result>
      </div>

      <div v-else-if="sessionStore.loading" class="history__side-body">
        <el-skeleton :rows="5" animated />
      </div>

      <div v-else-if="sessionStore.sessions.length === 0" class="history__side-body">
        <el-empty description="暂无会话" :image-size="80" />
      </div>

      <ul v-else class="history__list">
        <li
          v-for="item in sessionStore.sessions"
          :key="item.session_id"
          class="session"
          :class="{ 'session--active': item.session_id === activeSessionId }"
          @click="openSession(item.session_id)"
        >
          <!-- LED 在最左：一列会话扫下来，状态比标题更早进入视线 -->
          <span class="led session__led" :class="`led--${sessionTone(item)}`" aria-hidden="true" />
          <div class="session__body">
            <div class="session__title">{{ item.title || '未命名会话' }}</div>
            <div class="session__sub">{{ item.last_question || '—' }}</div>
            <div class="session__meta">
              <span class="tnum">{{ item.message_count }} 条</span>
              <span class="tnum">{{ formatTime(item.updated_at) }}</span>
            </div>
          </div>
        </li>
      </ul>

      <div v-if="sessionStore.total > sessionStore.limit" class="history__pager">
        <el-pagination
          layout="prev, pager, next"
          size="small"
          :current-page="sessionStore.page"
          :page-size="sessionStore.limit"
          :total="sessionStore.total"
          @current-change="onPageChange"
        />
      </div>
    </aside>

    <!-- 右：消息回看 -->
    <section class="history__main">
      <div v-if="!activeSessionId" class="history__placeholder">
        <el-empty description="从左侧选择一个会话查看历史消息" />
      </div>

      <template v-else>
        <div class="history__main-head">
          <div class="history__main-title-wrap">
            <div class="history__main-title">{{ detailTitle || '会话详情' }}</div>
            <div class="history__main-sid text-mono">{{ activeSessionId }}</div>
          </div>
          <el-button
            type="primary"
            size="small"
            :disabled="!!detailError"
            @click="continueChat"
          >
            继续对话
          </el-button>
        </div>

        <div class="history__scroll">
          <el-skeleton v-if="detailLoading" :rows="8" animated class="history__loading" />

          <el-result
            v-else-if="detailError"
            icon="warning"
            title="历史消息加载失败"
            :sub-title="`${detailError.code}：${humanizeError(detailError)}`"
          >
            <template #extra>
              <el-button size="small" type="primary" @click="openSession(activeSessionId)">
                重试
              </el-button>
            </template>
          </el-result>

          <el-empty v-else-if="detailMessages.length === 0" description="该会话暂无消息记录" />

          <div v-else class="history__messages">
            <div
              v-for="message in detailMessages"
              :key="`${message.qa_id}-${message.role}`"
              class="turn"
              :class="`turn--${message.role}`"
            >
              <div class="turn__head">
                <span class="turn__role" :class="`turn__role--${message.role}`">
                  {{ message.role === 'user' ? '提问' : '回答' }}
                </span>
                <span class="turn__meta tnum">{{ formatTime(message.created_at) }}</span>
                <span v-if="message.latency_ms !== null" class="turn__meta tnum">
                  {{ message.latency_ms }} ms
                </span>
                <span class="turn__meta text-mono">#{{ message.qa_id }}</span>
              </div>

              <!-- 助手回答走与问答页同一套 markdown，[n] 角标同样可点 -->
              <div
                v-if="message.role === 'assistant'"
                class="turn__content markdown-body"
                @click="onContentClick($event, message.citations)"
                v-html="renderMarkdown(message.content)"
              />
              <div v-else class="turn__content pre-wrap">{{ message.content }}</div>

              <div v-if="message.role === 'assistant'" class="turn__foot">
                <StatusBadge :status="message.status" />
                <ConfidenceTag :confidence="message.confidence" :label="message.confidence_label" />
              </div>

              <div v-if="message.citations?.length" class="turn__citations">
                <CitationCard
                  v-for="citation in message.citations"
                  :key="citation.id"
                  :citation="citation"
                  @open="openCitation"
                />
              </div>

              <!-- 历史消息带 qa_id，是唯一能提交反馈的场景 -->
              <FeedbackBar
                v-if="message.role === 'assistant'"
                :qa-id="message.qa_id"
              />
            </div>
          </div>
        </div>
      </template>
    </section>

    <SourceDrawer v-model="drawerVisible" :citation="activeCitation" />
  </div>
</template>

<style scoped>
.history {
  display: flex;
  flex: 1;
  min-height: 0;
  background: var(--surface-1);
}

/* ---------------------------------------------------------------- 左侧栏 */
.history__side {
  display: flex;
  flex: 0 0 312px;
  flex-direction: column;
  min-height: 0;
  background: var(--surface-0);
  border-right: 1px solid var(--line-1);
}

.history__side-head {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: var(--sp-2);
  padding: var(--sp-4) var(--sp-4) 0;
}

.history__side-title {
  font-size: var(--fs-base);
  font-weight: 600;
  color: var(--ink-900);
}

.history__count {
  padding: 1px 7px;
  font-size: 11px;
  font-weight: 600;
  color: var(--ink-500);
  background: var(--surface-2);
  border-radius: var(--r-pill);
}

.history__search {
  display: flex;
  flex: 0 0 auto;
  gap: var(--sp-2);
  padding: var(--sp-3) var(--sp-4);
}

/* 让错误态 / 空态 / 骨架在侧栏里垂直居中 */
.history__side-body {
  display: flex;
  flex: 1;
  align-items: center;
  justify-content: center;
  padding: 0 var(--sp-4) var(--sp-6);
  overflow-y: auto;
}

.history__list {
  flex: 1;
  padding: 0 var(--sp-3) var(--sp-3);
  margin: 0;
  overflow-y: auto;
  list-style: none;
}

/* ------------------------------------------------------------ 会话条目 */
/* 紧凑数据行：左侧 LED + 三行信息（标题 / 最近一问 / 条数与时间）。
   行高刻意压到 ~62px，一屏能看十几条会话，符合「仪表盘上翻列表」的用法 */
.session {
  position: relative;
  display: flex;
  gap: var(--sp-3);
  align-items: flex-start;
  padding: 9px var(--sp-3) 9px var(--sp-4);
  margin-bottom: 2px;
  cursor: pointer;
  border: 1px solid transparent;
  border-radius: var(--r-md);
  transition: background 0.14s var(--ease), border-color 0.14s var(--ease),
    box-shadow 0.14s var(--ease);
}

.session:hover {
  background: var(--surface-2);
}

.session--active {
  background: rgba(0, 212, 255, 0.06);
  border-color: rgba(0, 212, 255, 0.2);
  box-shadow: inset 0 0 20px rgba(0, 212, 255, 0.04);
}

/* 选中态左侧加一条主色竖条，与引用卡片的处理保持一致 */
.session--active::before {
  position: absolute;
  top: 50%;
  left: 0;
  width: 3px;
  height: 22px;
  content: '';
  background: var(--brand-600);
  border-radius: 0 2px 2px 0;
  box-shadow: 0 0 8px rgba(0, 212, 255, 0.6);
  transform: translateY(-50%);
}

/* 与标题首行的视觉中线对齐，不跟着 flex 顶对齐 */
.session__led {
  margin-top: 5px;
}

.session__body {
  flex: 1;
  min-width: 0;
}

.session__title {
  overflow: hidden;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--ink-900);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.session--active .session__title {
  color: var(--brand-400);
}

.session__sub {
  margin: 3px 0 5px;
  overflow: hidden;
  font-size: var(--fs-xs);
  color: var(--ink-500);
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 条数与时间用等宽数位：一列里数字的位数不同也不会有跳动感 */
.session__meta {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--ink-400);
}

.history__pager {
  display: flex;
  flex: 0 0 auto;
  justify-content: center;
  padding: var(--sp-2);
  border-top: 1px solid var(--line-1);
}

/* ---------------------------------------------------------------- 右侧 */
.history__main {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

.history__placeholder {
  display: flex;
  flex: 1;
  align-items: center;
  justify-content: center;
}

.history__main-head {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-3) var(--sp-5);
  background: var(--surface-0);
  border-bottom: 1px solid var(--line-1);
}

.history__main-title-wrap {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  min-width: 0;
}

.history__main-title {
  overflow: hidden;
  font-size: var(--fs-base);
  font-weight: 600;
  color: var(--ink-900);
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 与问答页顶栏的会话 id 同款：等宽 + 青字 + 青色描边底 */
.history__main-sid {
  flex: 0 0 auto;
  padding: 1px 7px;
  font-size: 11px;
  color: var(--brand-400);
  cursor: text;
  background: rgba(0, 212, 255, 0.07);
  border: 1px solid rgba(0, 212, 255, 0.16);
  border-radius: var(--r-sm);
  user-select: all;
}

.history__scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.history__loading {
  width: 100%;
  max-width: var(--conversation-w);
  padding: var(--sp-6) var(--sp-5);
  margin: 0 auto;
}

.history__messages {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  width: 100%;
  max-width: var(--conversation-w);
  padding: var(--sp-5) var(--sp-5) var(--sp-8);
  margin: 0 auto;
}

/* ------------------------------------------------------------ 单轮问答 */
.turn {
  padding: var(--sp-4) var(--sp-5);
  border: 1px solid var(--line-1);
  border-radius: var(--r-lg);
}

/* 提问用更亮一档的底（surface-2）且无描边，回答用 surface-0 + 描边：
   深色主题下靠明度差分层，一屏里能立刻分出问答对 */
.turn--user {
  background: var(--surface-2);
  border-color: transparent;
}

.turn--assistant {
  background: var(--surface-0);
  box-shadow: var(--sh-xs);
}

.turn__head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: var(--sp-2);
}

.turn__role {
  padding: 1px 8px;
  font-size: 11px;
  font-weight: 600;
  border-radius: var(--r-pill);
}

.turn__role--user {
  color: var(--ink-500);
  background: var(--surface-0);
  border: 1px solid var(--line-1);
}

.turn__role--assistant {
  color: var(--brand-400);
  background: var(--brand-50);
  border: 1px solid rgba(0, 212, 255, 0.2);
}

.turn__meta {
  font-size: 11px;
  color: var(--ink-400);
}

.turn__content {
  font-size: var(--fs-sm);
  line-height: 1.75;
  color: var(--ink-900);
}

.turn__foot {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
}

.turn__citations {
  display: grid;
  gap: var(--sp-2);
  margin-top: var(--sp-3);
  /* 宽屏时引用并排两列，避免一屏被引用卡片占满 */
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
}

.turn :deep(.feedback-bar) {
  margin-top: var(--sp-3);
}

@media (max-width: 900px) {
  .history__side {
    flex-basis: 232px;
  }
  .turn__citations {
    grid-template-columns: 1fr;
  }
}
</style>
