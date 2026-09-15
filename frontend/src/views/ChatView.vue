<script setup lang="ts">
/**
 * 问答页（FR-01/02/03/04/05/09）。
 *
 * 页面只负责三件事：装配子组件、持有 SourceDrawer 的单例、把「重试」转成一次新提问。
 * 所有状态都在 stores/chat.ts 里，页面自身不存业务数据。
 *
 * SourceDrawer 放在页面级而不是每条消息里：抽屉是全局唯一的面板，
 * 每条消息各持一个会产生 N 个隐藏抽屉实例，且同时只能看一个。
 */
import { ElMessage } from 'element-plus'
import { onBeforeUnmount, ref } from 'vue'
import { useRouter } from 'vue-router'

import type { Citation } from '@/api/chat'
import { humanizeError } from '@/api/http'
import ChatInput from '@/components/chat/ChatInput.vue'
import MessageList from '@/components/chat/MessageList.vue'
import SourceDrawer from '@/components/citation/SourceDrawer.vue'
import { abortActiveStream, useChatStore } from '@/stores/chat'
import { useSessionStore } from '@/stores/session'

const chatStore = useChatStore()
const sessionStore = useSessionStore()
const router = useRouter()

const drawerVisible = ref(false)
const activeCitation = ref<Citation | null>(null)

function openCitation(citation: Citation): void {
  activeCitation.value = citation
  drawerVisible.value = true
}

/**
 * 重试：
 *   - 带 question（示例问题按钮）→ 直接发这句；
 *   - 不带（消息上的「重试」）→ 重发最近一条用户提问。
 * 之所以不让 MessageItem 自己知道上一条提问：它只该渲染自己那一条。
 */
async function onRetry(question: string): Promise<void> {
  let text = question
  if (!text) {
    const lastUser = [...chatStore.messages].reverse().find((item) => item.role === 'user')
    text = lastUser?.content ?? ''
  }
  if (!text) return
  try {
    await chatStore.send(text)
  } catch (error) {
    ElMessage.error(humanizeError(error))
  }
}

function startNewSession(): void {
  chatStore.reset()
}

function goHistory(): void {
  void router.push('/history')
}

// 离开页面时中断在途的流：否则组件已卸载，回调仍在往 store 里写
onBeforeUnmount(() => {
  abortActiveStream()
})
</script>

<template>
  <div class="chat-view">
    <div class="chat-view__bar">
      <div class="chat-view__bar-inner">
        <div class="chat-view__title">
          <span class="chat-view__title-text">
            {{ sessionStore.currentTitle || (sessionStore.currentSessionId ? '当前会话' : '新会话') }}
          </span>
          <span v-if="sessionStore.currentSessionId" class="chat-view__sid text-mono">
            {{ sessionStore.currentSessionId }}
          </span>
        </div>
        <div class="chat-view__bar-actions">
          <el-button size="small" @click="goHistory">历史会话</el-button>
          <el-button size="small" type="primary" plain @click="startNewSession">新会话</el-button>
        </div>
      </div>

      <!-- 页面级错误：加载历史失败等。渲染明确的错误态而不是空列表 -->
      <el-alert
        v-if="chatStore.pageError"
        class="chat-view__error"
        type="error"
        :closable="true"
        show-icon
        :title="`加载失败：${chatStore.pageError.code}`"
        @close="chatStore.clearPageError()"
      >
        <p>{{ humanizeError(chatStore.pageError) }}</p>
      </el-alert>
    </div>

    <MessageList @open-citation="openCitation" @retry="onRetry" />

    <ChatInput />

    <SourceDrawer v-model="drawerVisible" :citation="activeCitation" />
  </div>
</template>

<style scoped>
.chat-view {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
  background: var(--surface-1);
}

.chat-view__bar {
  flex: 0 0 auto;
  background: var(--surface-0);
  border-bottom: 1px solid var(--line-1);
}

/* 与消息列同宽，标题和按钮才和下方内容左右对齐 */
.chat-view__bar-inner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-4);
  width: 100%;
  max-width: var(--conversation-w);
  padding: var(--sp-3) var(--sp-5);
  margin: 0 auto;
}

.chat-view__title {
  display: flex;
  align-items: center;
  gap: var(--sp-3);
  min-width: 0;
}

.chat-view__title-text {
  overflow: hidden;
  font-size: var(--fs-base);
  font-weight: 600;
  color: var(--ink-900);
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 会话 id 是排查用的次要信息，弱化到几乎看不见，但可选中复制 */
.chat-view__sid {
  flex: 0 0 auto;
  padding: 1px 7px;
  font-size: 11px;
  color: var(--ink-400);
  cursor: text;
  background: var(--surface-1);
  border-radius: var(--r-sm);
  user-select: all;
}

.chat-view__bar-actions {
  display: flex;
  flex: 0 0 auto;
  gap: var(--sp-2);
}

.chat-view__error {
  max-width: var(--conversation-w);
  margin: 0 auto var(--sp-3);
  border-radius: var(--r-md);
}

@media (max-width: 720px) {
  .chat-view__bar-inner {
    padding-right: var(--sp-4);
    padding-left: var(--sp-4);
  }
}
</style>
