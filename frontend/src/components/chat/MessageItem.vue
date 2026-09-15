<script setup lang="ts">
/**
 * 单条消息渲染（流式追加、引用角标、置信度、反馈）。
 *
 * 渲染规则（markdown 实例与引用角标的实现见 utils/markdown.ts）：
 *
 * 1. **助手消息走 markdown**，正文里的 `[n]` 变成可点击角标，点击后按 id 找到引用。
 * 2. **用户消息不走 markdown**，纯文本 `pre-wrap`。用户输入的 `<b>` 应当原样显示，
 *    而且用户消息里也没有引用角标需要处理。
 *
 * 视觉分工：助手消息是白卡片（承载正文与引用清单），用户消息是右侧主色气泡。
 * 两者样式差别足够大，扫一眼就知道哪句是自己问的。
 *
 * 性能说明：流式期间每来一个 token 就重渲染整段 markdown，复杂度是 O(n²)。
 * 接口文档的答案长度在数千字量级，实测无感，因此不做增量渲染；
 * 若将来答案显著变长，再改成 rAF 合批即可（见 store 里同款说明）。
 */
import { CopyDocument, Refresh, Select, WarningFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'

import type { Citation } from '@/api/chat'
import { formatTime, humanizeError } from '@/api/http'
import CitationCard from '@/components/citation/CitationCard.vue'
import ImageThumb from '@/components/chat/ImageThumb.vue'
import ConfidenceTag from '@/components/common/ConfidenceTag.vue'
import FeedbackBar from '@/components/common/FeedbackBar.vue'
import StatusBadge from '@/components/common/StatusBadge.vue'
import type { UiMessage } from '@/stores/chat'
import { citationIdFromEvent, renderMarkdown } from '@/utils/markdown'

const props = defineProps<{ message: UiMessage }>()

const emit = defineEmits<{
  'open-citation': [citation: Citation]
  retry: [question: string]
}>()

const isUser = computed(() => props.message.role === 'user')
const isStreaming = computed(
  () => props.message.phase === 'streaming' || props.message.phase === 'connecting',
)
const showCitations = computed(() => !isUser.value && props.message.citations.length > 0)
/** 低置信度时只给材料清单，不给操作步骤（接口文档 §3.2） */
const showLowConfidenceHint = computed(
  () => !isUser.value && props.message.confidenceLabel === 'low' && props.message.status !== 'ERROR',
)

// 流式期间 content 每个 token 都在变，computed 会自动跟随。
// 渲染器与历史页共用（utils/markdown.ts）：同一条答案在两处的呈现必须一致。
const rendered = computed(() => renderMarkdown(props.message.content))

// --------------------------------------------------------------------------- //
// 交互
// --------------------------------------------------------------------------- //

/** 事件委托：处理点开的引用角标（按 id 查，见 utils/markdown.ts 的说明） */
function onContentClick(event: MouseEvent): void {
  const id = citationIdFromEvent(event)
  if (id === null) return

  const citation = props.message.citations.find((item) => item.id === id)
  if (citation) {
    emit('open-citation', citation)
    return
  }
  // 流式过程中答案可能先于 citations 事件到达，此时角标还没有对应数据
  ElMessage.info(`引用 [${id}] 的依据尚未返回，请等本次回答结束。`)
}

/** 重试：把上一条用户提问交回父组件重发 */
function onRetry(): void {
  emit('retry', '')
}

const copied = ref(false)
async function copyAnswer(): Promise<void> {
  try {
    await navigator.clipboard.writeText(props.message.content)
    copied.value = true
    window.setTimeout(() => (copied.value = false), 1500)
  } catch {
    ElMessage.warning('复制失败，请手动选中复制。')
  }
}
</script>

<template>
  <div class="msg" :class="isUser ? 'msg--user' : 'msg--assistant'">
    <!-- 头像 -->
    <div class="msg__avatar" :class="isUser ? 'msg__avatar--user' : 'msg__avatar--ai'">
      <span v-if="isUser">我</span>
      <svg v-else viewBox="0 0 24 24" width="15" height="15" fill="none" aria-hidden="true">
        <rect
          x="6.5"
          y="6.5"
          width="11"
          height="11"
          rx="2.5"
          stroke="currentColor"
          stroke-width="1.8"
        />
        <rect x="10.4" y="10.4" width="3.2" height="3.2" rx="0.9" fill="currentColor" />
      </svg>
    </div>

    <div class="msg__body">
      <!-- ------------------------------------------------ 用户消息 -->
      <template v-if="isUser">
        <div class="msg__bubble pre-wrap">{{ message.content }}</div>

        <div v-if="message.images.length" class="msg__images">
          <ImageThumb v-for="img in message.images" :key="img.localId" :image="img" :size="84" />
        </div>

        <div class="msg__stamp tnum">{{ formatTime(message.createdAt) }}</div>
      </template>

      <!-- ------------------------------------------------ 助手消息 -->
      <template v-else>
        <!-- 图片识别结果（SSE image 事件，发生在正文之前） -->
        <div v-if="message.imageResult" class="msg__image-result">
          <div class="msg__image-result-head">
            <span class="msg__image-result-type">{{ message.imageResult.image_type || 'unknown' }}</span>
            <span class="msg__image-result-conf tnum">
              置信度 {{ Math.round(message.imageResult.confidence * 100) }}%
            </span>
          </div>
          <div
            v-if="Object.keys(message.imageResult.extracted).length"
            class="msg__image-extracted"
          >
            <span
              v-for="(value, key) in message.imageResult.extracted"
              :key="key"
              class="msg__image-chip"
            >
              {{ key }}: {{ typeof value === 'string' ? value : JSON.stringify(value) }}
            </span>
          </div>
        </div>

        <!-- 正文 -->
        <div
          v-if="message.content"
          class="msg__card markdown-body"
          @click="onContentClick"
          v-html="rendered"
        />
        <div v-else-if="isStreaming" class="msg__card msg__card--pending">
          <span class="msg__thinking">
            <span class="msg__thinking-dot" />
            <span class="msg__thinking-dot" />
            <span class="msg__thinking-dot" />
          </span>
          <span>正在检索知识库并组织答案…</span>
        </div>

        <!-- 流式光标（跟在正文之后） -->
        <div v-if="isStreaming && message.content" class="msg__caret-row">
          <span class="stream-caret" />
        </div>

        <!-- 中断提示：不是错误，中性色 -->
        <p v-if="message.phase === 'aborted'" class="msg__hint">已停止生成，以上为已接收到的内容</p>

        <!-- 拒答提示（FR-05：不给操作步骤） -->
        <div v-if="message.status === 'NOT_COVERED'" class="msg__note msg__note--info">
          <p class="msg__note-title">知识库未覆盖该问题</p>
          <p class="msg__note-text">
            系统未检索到可靠依据，按零幻觉策略不给出操作步骤。请补充设备型号、报警代码，或改用文字描述现象。
          </p>
        </div>

        <!-- 中置信度：需人工确认 -->
        <div v-if="message.confidenceLabel === 'medium'" class="msg__note msg__note--warn">
          <p class="msg__note-title">结论需人工确认</p>
          <p class="msg__note-text">
            检索证据基本相关但覆盖不全，请对照下方引用原文复核后再执行。
          </p>
        </div>

        <!-- 低置信度：只给材料清单 -->
        <div v-if="showLowConfidenceHint" class="msg__note msg__note--info">
          <p class="msg__note-title">低置信度：仅提供相关材料清单</p>
          <p class="msg__note-text">
            未达到可执行结论的阈值，请以引用原文为准，不要按推测操作。
          </p>
        </div>

        <!-- 链路异常 -->
        <div v-if="message.phase === 'error' && message.error" class="msg__note msg__note--error">
          <p class="msg__note-title msg__note-title--error">
            <el-icon><WarningFilled /></el-icon>
            生成失败：{{ message.error.code }}
          </p>
          <p class="msg__note-text">{{ humanizeError(message.error) }}</p>
          <p v-if="message.error.traceId" class="msg__note-trace text-mono">
            trace_id: {{ message.error.traceId }}
          </p>
          <el-button size="small" type="primary" plain :icon="Refresh" @click="onRetry">
            重试
          </el-button>
        </div>

        <!-- 存疑点 -->
        <div v-if="message.uncertain.length" class="msg__uncertain">
          <p class="msg__uncertain-title">以下内容存疑，请人工核实</p>
          <ul>
            <li v-for="(item, index) in message.uncertain" :key="index">{{ item }}</li>
          </ul>
        </div>

        <!-- 引用清单（FR-04） -->
        <div v-if="showCitations" class="msg__citations">
          <div class="msg__citations-head">
            <span class="msg__citations-title">引用依据</span>
            <span class="msg__citations-count tnum">{{ message.citations.length }}</span>
          </div>
          <div class="msg__citations-list">
            <CitationCard
              v-for="citation in message.citations"
              :key="citation.id"
              :citation="citation"
              @open="emit('open-citation', $event)"
            />
          </div>
        </div>

        <!-- 页脚：状态 / 置信度 / 反馈 -->
        <div v-if="!isStreaming" class="msg__footer">
          <div class="msg__footer-meta">
            <StatusBadge :status="message.status" />
            <ConfidenceTag :confidence="message.confidence" :label="message.confidenceLabel" />
            <span v-if="message.latencyMs !== null" class="msg__meta-item tnum">
              {{ message.latencyMs }} ms
            </span>
            <span v-if="message.traceId" class="msg__meta-item text-mono" :title="message.traceId">
              {{ message.traceId.slice(0, 8) }}
            </span>
          </div>
          <el-button
            v-if="message.content"
            class="msg__copy"
            size="small"
            text
            :icon="copied ? Select : CopyDocument"
            @click="copyAnswer"
          >
            {{ copied ? '已复制' : '复制' }}
          </el-button>
        </div>

        <FeedbackBar
          v-if="!isStreaming && message.content"
          :qa-id="message.qaId"
          :submitted="message.feedbackSubmitted"
        />
      </template>
    </div>
  </div>
</template>

<style scoped>
.msg {
  display: flex;
  gap: var(--sp-3);
  align-items: flex-start;
}

.msg--user {
  flex-direction: row-reverse;
}

/* ------------------------------------------------------------------ 头像 */
.msg__avatar {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  font-size: var(--fs-xs);
  font-weight: 600;
  border-radius: 9px;
}

.msg__avatar--ai {
  color: #fff;
  background: linear-gradient(140deg, var(--brand-500), var(--brand-700));
  box-shadow: 0 2px 6px rgb(37 99 235 / 22%);
}

.msg__avatar--user {
  color: var(--ink-500);
  background: var(--surface-2);
  border: 1px solid var(--line-1);
}

/* -------------------------------------------------------------------- 主体 */
/*
 * 助手侧必须 flex: 1：卡片是 width:100%，若父元素宽度由内容决定，
 * 卡片的 100% 就成了一道循环引用，宽屏下会明显窄于对话列宽。
 * 用户侧反过来要按内容收缩（flex: 0 1 auto），只靠 max-width 限制最宽。
 */
.msg__body {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}

.msg--user .msg__body {
  flex: 0 1 auto;
  align-items: flex-end;
  max-width: 76%;
}

/* 助手消息：白卡片承载正文，与页面的浅灰底形成层次 */
.msg__card {
  width: 100%;
  padding: var(--sp-4) var(--sp-5);
  background: var(--surface-0);
  border: 1px solid var(--line-1);
  border-radius: var(--r-lg);
  box-shadow: var(--sh-xs);
}

.msg__card--pending {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: var(--fs-sm);
  color: var(--ink-400);
}

/* ------------------------------------------------------------------ 气泡 */
.msg__bubble {
  padding: 10px var(--sp-4);
  font-size: var(--fs-md);
  line-height: 1.65;
  color: #fff;
  background: linear-gradient(140deg, var(--brand-500), var(--brand-600));
  border-radius: var(--r-lg) var(--r-lg) 4px var(--r-lg);
  box-shadow: 0 2px 8px rgb(37 99 235 / 18%);
}

.msg__stamp {
  padding-right: 2px;
  font-size: 11px;
  color: var(--ink-400);
}

.msg__images {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  justify-content: flex-end;
}

/* -------------------------------------------------------------- 思考动画 */
.msg__thinking {
  display: inline-flex;
  gap: 4px;
}

.msg__thinking-dot {
  width: 5px;
  height: 5px;
  background: var(--brand-400);
  border-radius: 50%;
  animation: msg-bounce 1.2s ease-in-out infinite;
}
.msg__thinking-dot:nth-child(2) {
  animation-delay: 0.15s;
}
.msg__thinking-dot:nth-child(3) {
  animation-delay: 0.3s;
}

@keyframes msg-bounce {
  0%,
  60%,
  100% {
    opacity: 0.35;
    transform: translateY(0);
  }
  30% {
    opacity: 1;
    transform: translateY(-3px);
  }
}

.msg__caret-row {
  margin-top: -6px;
  padding-left: var(--sp-5);
}

.msg__hint {
  margin: 0;
  padding-left: 2px;
  font-size: var(--fs-xs);
  color: var(--ink-400);
}

/* -------------------------------------------------------------- 提示条 */
/* 不用 el-alert：它的图标列和背景色在浅色页面里偏重，这里统一成描边卡片 */
.msg__note {
  width: 100%;
  padding: 10px var(--sp-4);
  border: 1px solid;
  border-radius: var(--r-md);
}

.msg__note--info {
  background: var(--info-50);
  border-color: #e3e7ee;
}
.msg__note--warn {
  background: var(--warn-50);
  border-color: #f5e3c0;
}
.msg__note--error {
  background: var(--danger-50);
  border-color: #f7d4d1;
}

.msg__note-title {
  display: flex;
  align-items: center;
  gap: 5px;
  margin: 0 0 3px;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--info-600);
}
.msg__note--warn .msg__note-title {
  color: var(--warn-600);
}
.msg__note-title--error {
  color: var(--danger-600);
}

.msg__note-text {
  margin: 0;
  font-size: var(--fs-sm);
  line-height: 1.6;
  color: var(--ink-500);
}

.msg__note-trace {
  margin: 6px 0 8px;
  color: var(--ink-400);
}

/* ------------------------------------------------------------ 图片识别 */
.msg__image-result {
  width: 100%;
  padding: 10px var(--sp-4);
  background: var(--brand-50);
  border: 1px solid var(--brand-100);
  border-radius: var(--r-md);
}

.msg__image-result-head {
  display: flex;
  align-items: baseline;
  gap: var(--sp-3);
}

.msg__image-result-type {
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--brand-700);
}

.msg__image-result-conf {
  font-size: var(--fs-xs);
  color: var(--brand-600);
}

.msg__image-extracted {
  display: flex;
  flex-wrap: wrap;
  gap: 5px;
  margin-top: 7px;
}

.msg__image-chip {
  padding: 2px 7px;
  font-size: 11px;
  color: var(--ink-700);
  background: rgb(255 255 255 / 80%);
  border: 1px solid var(--brand-100);
  border-radius: var(--r-sm);
}

/* ---------------------------------------------------------------- 存疑点 */
.msg__uncertain {
  width: 100%;
  padding: 10px var(--sp-4);
  background: var(--warn-50);
  border: 1px solid #f5e3c0;
  border-radius: var(--r-md);
}

.msg__uncertain-title {
  margin: 0 0 4px;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--warn-600);
}

.msg__uncertain ul {
  margin: 0;
  padding-left: 18px;
  font-size: var(--fs-sm);
  color: var(--ink-700);
}

/* ------------------------------------------------------------------ 引用 */
.msg__citations {
  width: 100%;
}

.msg__citations-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: var(--sp-2);
  padding-left: 2px;
}

.msg__citations-title {
  font-size: var(--fs-xs);
  font-weight: 600;
  color: var(--ink-500);
  letter-spacing: 0.3px;
}

.msg__citations-count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 17px;
  height: 17px;
  padding: 0 5px;
  font-size: 11px;
  font-weight: 600;
  color: var(--brand-600);
  background: var(--brand-50);
  border-radius: var(--r-pill);
}

.msg__citations-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}

/* ------------------------------------------------------------------ 页脚 */
.msg__footer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-3);
  padding: 2px;
}

.msg__footer-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
}

.msg__meta-item {
  font-size: var(--fs-xs);
  color: var(--ink-400);
}

/* 复制按钮默认低调，hover 才显形，避免每轮回答都挂个按钮抢注意力 */
.msg__copy {
  margin-left: auto;
  font-size: var(--fs-xs);
  color: var(--ink-400);
  opacity: 0.85;
}

.msg__copy:hover {
  color: var(--brand-600);
  background: var(--brand-50);
}

@media (max-width: 720px) {
  .msg--user .msg__body {
    max-width: 88%;
  }
}
</style>
