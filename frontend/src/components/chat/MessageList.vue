<script setup lang="ts">
/**
 * 消息列表：滚动管理 + 空态 + 骨架。
 *
 * 自动滚动刻意做了「用户是否贴着底部」的判断：流式输出时如果无条件吸底，
 * 用户往上翻看引用原文就会被不断拽回底部——这是流式界面最常见的体验事故。
 *
 * 布局用「内层限宽列」而不是给滚动容器加 padding：
 * 滚动条必须贴在窗口右缘，若把限宽加在滚动容器上，宽屏下滚动条会浮在页面中间。
 */
import { ArrowRight } from '@element-plus/icons-vue'
import { computed, nextTick, onMounted, ref, watch } from 'vue'

import type { Citation } from '@/api/chat'
import MessageItem from '@/components/chat/MessageItem.vue'
import { useChatStore } from '@/stores/chat'

const emit = defineEmits<{
  'open-citation': [citation: Citation]
  retry: [question: string]
}>()

const chatStore = useChatStore()

const scroller = ref<HTMLElement | null>(null)
/** 用户是否停留在底部附近。只有贴底时才自动跟随新内容 */
const stickToBottom = ref(true)
/** 用户往上翻之后，右下角浮出「回到底部」 */
const showJump = ref(false)

/** 流式期间最后一条消息的 content 每个 token 都变，用它驱动自动滚动 */
const contentSignal = computed(() => {
  const last = chatStore.messages[chatStore.messages.length - 1]
  return last ? last.content.length : 0
})

function onScroll(): void {
  const el = scroller.value
  if (!el) return
  // 距底部 80px 以内视为「贴着底」，留一点容差避免像素级抖动
  stickToBottom.value = el.scrollHeight - el.scrollTop - el.clientHeight < 80
  showJump.value = el.scrollHeight - el.scrollTop - el.clientHeight > 320
}

async function scrollToBottom(force = false): Promise<void> {
  if (!force && !stickToBottom.value) return
  await nextTick()
  const el = scroller.value
  if (el) el.scrollTop = el.scrollHeight
}

watch(() => chatStore.messages.length, () => void scrollToBottom(true))
watch(contentSignal, () => void scrollToBottom())
// 切换会话时强制跳到最新一条
watch(
  () => chatStore.messages[0]?.localId,
  () => void scrollToBottom(true),
)

onMounted(() => void scrollToBottom(true))

/**
 * 空态示例问题：后端不可用时也给出可点击的入口，避免页面看起来是坏的。
 * 分三组覆盖 FR-01（故障排查）/ FR-03（参数规格）/ 元数据检索三类典型问法。
 */
const examples = [
  {
    tag: '故障排查',
    text: '刻蚀机腔体真空度异常波动，应该按什么顺序排查？',
  },
  {
    tag: '维护周期',
    text: 'Etcher-A 的 O-ring 更换周期是多少？',
  },
  {
    tag: '报警代码',
    text: '设备报 E-2041 报警，可能的原因有哪些？',
  },
]

function useExample(text: string): void {
  emit('retry', text)
}
</script>

<template>
  <div ref="scroller" class="list" @scroll="onScroll">
    <!-- 加载历史会话时的骨架屏 -->
    <div v-if="chatStore.loadingHistory" class="list__inner">
      <div v-for="i in 3" :key="i" class="list__skeleton">
        <el-skeleton animated>
          <template #template>
            <el-skeleton-item variant="text" style="width: 34%; height: 15px" />
            <el-skeleton-item
              variant="text"
              style="width: 100%; height: 13px; margin-top: 14px"
            />
            <el-skeleton-item variant="text" style="width: 86%; height: 13px; margin-top: 8px" />
            <el-skeleton-item variant="text" style="width: 62%; height: 13px; margin-top: 8px" />
          </template>
        </el-skeleton>
      </div>
    </div>

    <!-- 空态 -->
    <div v-else-if="!chatStore.hasMessages" class="list__inner list__inner--empty">
      <div class="empty">
        <div class="empty__mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="26" height="26" fill="none">
            <rect
              x="6.5"
              y="6.5"
              width="11"
              height="11"
              rx="2.5"
              stroke="currentColor"
              stroke-width="1.6"
            />
            <rect x="10.2" y="10.2" width="3.6" height="3.6" rx="1" fill="currentColor" />
            <path
              d="M9.5 3.2v3.3M14.5 3.2v3.3M9.5 17.5v3.3M14.5 17.5v3.3M3.2 9.5h3.3M3.2 14.5h3.3M17.5 9.5h3.3M17.5 14.5h3.3"
              stroke="currentColor"
              stroke-width="1.6"
              stroke-linecap="round"
            />
          </svg>
        </div>

        <h2 class="empty__title">今天要排查什么设备问题？</h2>
        <p class="empty__desc">
          可以问故障现象、参数规格、维护周期与备件信息，也可以直接粘贴一张报警截图。
          <br />
          每条结论都会附上手册出处，未检索到依据时会明确告知，不做推测。
        </p>

        <div class="empty__examples">
          <button
            v-for="item in examples"
            :key="item.text"
            class="empty__card"
            type="button"
            @click="useExample(item.text)"
          >
            <span class="empty__card-tag">{{ item.tag }}</span>
            <span class="empty__card-text">{{ item.text }}</span>
            <el-icon class="empty__card-arrow"><ArrowRight /></el-icon>
          </button>
        </div>
      </div>
    </div>

    <!-- 消息 -->
    <div v-else class="list__inner">
      <MessageItem
        v-for="message in chatStore.messages"
        :key="message.localId"
        :message="message"
        @open-citation="emit('open-citation', $event)"
        @retry="emit('retry', $event)"
      />
    </div>

    <!-- 回到底部：用户翻到上面时，流式内容继续增长会失去位置感 -->
    <transition name="jump">
      <button
        v-if="showJump"
        class="list__jump"
        type="button"
        title="回到底部"
        @click="scrollToBottom(true)"
      >
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none">
          <path
            d="M12 5v13m0 0l-5-5m5 5l5-5"
            stroke="currentColor"
            stroke-width="1.9"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
        </svg>
      </button>
    </transition>
  </div>
</template>

<style scoped>
.list {
  position: relative;
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  background: var(--surface-1);
}

/* 限宽列：对话框和空态都走这个宽度，宽屏下正文不再拉成一条长线 */
.list__inner {
  display: flex;
  flex-direction: column;
  gap: var(--sp-5);
  width: 100%;
  max-width: var(--conversation-w);
  padding: var(--sp-6) var(--sp-5) var(--sp-8);
  margin: 0 auto;
}

.list__inner--empty {
  align-items: center;
  justify-content: center;
  min-height: 100%;
  padding-top: var(--sp-10);
  padding-bottom: var(--sp-10);
}

.list__skeleton {
  padding: var(--sp-4) var(--sp-5);
  background: var(--surface-0);
  border: 1px solid var(--line-1);
  border-radius: var(--r-lg);
}

/* ------------------------------------------------------------------ 空态 */
.empty {
  max-width: 620px;
  text-align: center;
}

.empty__mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 56px;
  height: 56px;
  color: #fff;
  background: linear-gradient(140deg, var(--brand-500), var(--brand-700));
  border-radius: var(--r-xl);
  box-shadow: 0 8px 20px rgb(37 99 235 / 22%);
}

.empty__title {
  margin: var(--sp-5) 0 var(--sp-2);
  font-size: var(--fs-xl);
  font-weight: 650;
  letter-spacing: -0.4px;
  color: var(--ink-900);
}

.empty__desc {
  margin: 0;
  font-size: var(--fs-base);
  line-height: 1.75;
  color: var(--ink-500);
}

.empty__examples {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  margin-top: var(--sp-8);
  text-align: left;
}

/* 示例问题做成整行可点的卡片，比小按钮更容易扫读也更好点 */
.empty__card {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  width: 100%;
  padding: 13px var(--sp-4);
  font-family: inherit;
  font-size: var(--fs-base);
  color: var(--ink-700);
  text-align: left;
  cursor: pointer;
  background: var(--surface-0);
  border: 1px solid var(--line-1);
  border-radius: var(--r-md);
  box-shadow: var(--sh-xs);
  transition: border-color 0.16s var(--ease), box-shadow 0.16s var(--ease),
    transform 0.16s var(--ease), color 0.16s var(--ease);
}

.empty__card:hover {
  color: var(--ink-900);
  border-color: var(--brand-200);
  box-shadow: var(--sh-md);
  transform: translateY(-1px);
}

.empty__card-tag {
  flex: 0 0 auto;
  padding: 2px 8px;
  font-size: 11px;
  font-weight: 600;
  color: var(--brand-600);
  background: var(--brand-50);
  border-radius: var(--r-pill);
}

.empty__card-text {
  flex: 1;
  min-width: 0;
}

.empty__card-arrow {
  flex: 0 0 auto;
  color: var(--ink-300);
  transition: color 0.16s var(--ease), transform 0.16s var(--ease);
}

.empty__card:hover .empty__card-arrow {
  color: var(--brand-600);
  transform: translateX(2px);
}

/* ------------------------------------------------------------ 回到底部 */
/* sticky 在这个滚动容器内生效：元素自然位置在内容末尾，
   往上滚时它就贴在视口底部。定宽 + margin auto 即可水平居中 */
.list__jump {
  position: sticky;
  bottom: var(--sp-4);
  display: flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  margin: 0 auto;
  color: var(--ink-500);
  cursor: pointer;
  background: var(--surface-0);
  border: 1px solid var(--line-1);
  border-radius: 50%;
  box-shadow: var(--sh-md);
}

.list__jump:hover {
  color: var(--brand-600);
  border-color: var(--brand-200);
}

.jump-enter-active,
.jump-leave-active {
  transition: opacity 0.18s var(--ease), transform 0.18s var(--ease);
}
.jump-enter-from,
.jump-leave-to {
  opacity: 0;
  transform: translateY(6px);
}

@media (max-width: 720px) {
  .list__inner {
    padding-right: var(--sp-4);
    padding-left: var(--sp-4);
  }
  .empty__examples {
    margin-top: var(--sp-6);
  }
}
</style>
