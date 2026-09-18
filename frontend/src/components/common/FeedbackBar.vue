<script setup lang="ts">
/**
 * 四态反馈（FR-07，接口文档 §4.6）：采纳 / 部分采纳 / 驳回 / 修正。
 *
 * 后端契约要点：
 *   - 必须带 qa_id；type=correct 时 corrected_answer 必填，否则 400 CORRECTED_ANSWER_REQUIRED；
 *   - comment 上限 500 字，超长会被 Pydantic 拦成 400。
 *
 * **qa_id 来源**：done 事件已带 qa_id（接口文档 §8 缺口③，后端 `DoneEvent.qa_id`），
 * chat store 在 onDone 里写进 message.qaId，所以刚生成完的回答也能直接反馈，不必刷新历史页。
 * 仅当后端确实没给出 id（落库失败等）时 qaId 才是 null，此时置灰并说明原因，
 * 而不是伪造一个 id 让请求 404。历史页的消息则由接口直接返回 qa_id。
 */
import { ElMessage } from 'element-plus'
import { computed, ref } from 'vue'

import { FEEDBACK_LABELS, submitFeedback, type FeedbackType } from '@/api/feedback'
import { humanizeError } from '@/api/http'

const props = defineProps<{
  qaId: number | null
  /** 已提交的反馈类型；非空时按钮进入只读态 */
  submitted?: string | null
  /** 链路异常 / 拒答的回答仍允许反馈（驳回正是这类场景的主要用途） */
  size?: 'small' | 'default'
}>()

const emit = defineEmits<{ submitted: [type: FeedbackType] }>()

const COMMENT_MAX = 500

const dialogVisible = ref(false)
const activeType = ref<FeedbackType>('adopt')
const comment = ref('')
const correctedAnswer = ref('')
const submitting = ref(false)

const disabled = computed(() => props.qaId === null)
const disabledReason = computed(() =>
  props.qaId === null
    ? '本次回答未返回 qa_id（后端未落库），请刷新「历史会话」后再对该轮问答提交反馈'
    : '',
)

const types: FeedbackType[] = ['adopt', 'partial', 'reject', 'correct']

/**
 * 色调映射：驳回用红、修正用琥珀、采纳用绿、部分采纳用蓝。
 * 这里的键值只作为 CSS 类名后缀（.feedback-bar__btn--danger 等），
 * 不直接传给 el-button 的 type —— 我们要的是 hover 才上色，
 * 而 EP 的 type 按钮默认就是实心彩底，四个连排会非常吵。
 */
const buttonType: Record<FeedbackType, 'primary' | 'success' | 'warning' | 'danger' | 'info'> = {
  adopt: 'success',
  partial: 'primary',
  reject: 'danger',
  correct: 'warning',
}

function open(type: FeedbackType): void {
  if (disabled.value) return
  activeType.value = type
  comment.value = ''
  correctedAnswer.value = ''
  dialogVisible.value = true
}

async function confirm(): Promise<void> {
  if (props.qaId === null) return

  // 客户端预校验，镜像后端的 400 CORRECTED_ANSWER_REQUIRED
  if (activeType.value === 'correct' && !correctedAnswer.value.trim()) {
    ElMessage.warning('反馈类型为「修正」时必须填写修正内容。')
    return
  }
  if (comment.value.length > COMMENT_MAX) {
    ElMessage.warning(`备注不能超过 ${COMMENT_MAX} 字。`)
    return
  }

  submitting.value = true
  try {
    await submitFeedback({
      qa_id: props.qaId,
      type: activeType.value,
      comment: comment.value.trim() || null,
      corrected_answer: correctedAnswer.value.trim() || null,
    })
    ElMessage.success('反馈已记录，感谢补充')
    dialogVisible.value = false
    emit('submitted', activeType.value)
  } catch (error) {
    ElMessage.error(humanizeError(error))
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="feedback-bar">
    <span v-if="!submitted" class="feedback-bar__label">这条回答有帮助吗？</span>

    <el-tooltip :content="disabledReason" :disabled="!disabled" placement="top">
      <span class="feedback-bar__group">
        <el-button
          v-for="type in types"
          :key="type"
          class="feedback-bar__btn"
          :class="[
            `feedback-bar__btn--${buttonType[type]}`,
            { 'feedback-bar__btn--picked': submitted === type },
          ]"
          :size="size ?? 'small'"
          :disabled="disabled || !!submitted"
          @click="open(type)"
        >
          {{ FEEDBACK_LABELS[type] }}
        </el-button>
      </span>
    </el-tooltip>

    <span v-if="submitted" class="feedback-bar__done">
      已反馈「{{ FEEDBACK_LABELS[submitted as FeedbackType] ?? submitted }}」
    </span>

    <el-dialog v-model="dialogVisible" :title="`反馈：${FEEDBACK_LABELS[activeType]}`" width="480px">
      <el-form label-position="top">
        <el-form-item v-if="activeType === 'correct'" label="修正后的答案（必填）">
          <el-input
            v-model="correctedAnswer"
            type="textarea"
            :rows="4"
            placeholder="请输入你认为正确的答案，将用于 badcase 归因与提示词优化"
          />
        </el-form-item>
        <el-form-item label="备注（可选）">
          <el-input
            v-model="comment"
            type="textarea"
            :rows="3"
            :maxlength="COMMENT_MAX"
            show-word-limit
            placeholder="例如：步骤 3 的参数下限与现场实际不符"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="confirm">提交反馈</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.feedback-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-2);
  padding: 2px;
}

.feedback-bar__label {
  font-size: var(--fs-xs);
  color: var(--ink-400);
}

.feedback-bar__group {
  display: inline-flex;
  gap: 4px;
}

/* 默认无底色，hover / 选中才上色：反馈是低频操作，不该常驻抢视觉 */
.feedback-bar__btn {
  font-size: var(--fs-xs);
  color: var(--ink-500);
  background: transparent;
  border-color: transparent;
}

.feedback-bar__btn:hover:not(:disabled) {
  background: var(--surface-2);
}

.feedback-bar__btn--success:hover:not(:disabled) {
  color: var(--ok-600);
  background: var(--ok-50);
}
.feedback-bar__btn--primary:hover:not(:disabled) {
  color: var(--brand-600);
  background: var(--brand-50);
}
.feedback-bar__btn--danger:hover:not(:disabled) {
  color: var(--danger-600);
  background: var(--danger-50);
}
.feedback-bar__btn--warning:hover:not(:disabled) {
  color: var(--warn-600);
  background: var(--warn-50);
}

/* 已提交的那一项保留底色，其余由 disabled 态压暗 */
.feedback-bar__btn--picked {
  border-color: currentcolor;
}

.feedback-bar__btn--picked.feedback-bar__btn--success {
  color: var(--ok-600);
  background: var(--ok-50);
}
.feedback-bar__btn--picked.feedback-bar__btn--primary {
  color: var(--brand-600);
  background: var(--brand-50);
}
.feedback-bar__btn--picked.feedback-bar__btn--danger {
  color: var(--danger-600);
  background: var(--danger-50);
}
.feedback-bar__btn--picked.feedback-bar__btn--warning {
  color: var(--warn-600);
  background: var(--warn-50);
}

.feedback-bar__done {
  font-size: var(--fs-xs);
  color: var(--ink-400);
}
</style>
