<script setup lang="ts">
/**
 * 置信度标签（FR-05，接口文档 §3.2）。
 *
 * 三级语义不能只靠颜色区分，必须带文字，否则色弱用户读不出差别：
 *   high   >= 0.80                 直接给结论
 *   medium 0.60 – 0.80             给结论 + 标注「需人工确认」
 *   low    < 0.60                  只给相关材料清单，不给操作步骤
 *
 * 注意 medium 的「需人工确认」不是装饰：这是零幻觉策略里对用户的显式提醒，
 * 不要把文案改成「中等置信度」这类中性描述。
 *
 * 用自绘 pill 而不是 el-tag：EP 的标签在页脚这一排里内边距偏大、色彩偏饱和，
 * 一排标签连着放会显得吵闹。
 */
import { computed } from 'vue'

import type { ConfidenceLabel } from '@/api/chat'

const props = defineProps<{
  confidence: number | null
  label: ConfidenceLabel | null
}>()

/**
 * 阈值只写进提示文案，**不在前端做判定**：
 * label 由后端 verify 节点按 0.40×精排分 + 0.35×引用覆盖率 + 0.25×来源权威度 算出，
 * 前端再算一遍会出现两边不一致的情况。
 */
const meta = computed(() => {
  switch (props.label) {
    case 'high':
      return { tone: 'high', text: '高置信度', hint: '≥ 0.80：可直接采用' }
    case 'medium':
      return { tone: 'medium', text: '需人工确认', hint: '0.60–0.80：结论需工程师复核' }
    case 'low':
      return { tone: 'low', text: '低置信度', hint: '< 0.60：仅提供相关材料，不提供操作步骤' }
    default:
      return null
  }
})

const percent = computed(() =>
  props.confidence === null || props.confidence === undefined
    ? null
    : `${Math.round(props.confidence * 100)}%`,
)

const tooltip = computed(() =>
  meta.value ? `${meta.value.hint}（当前 ${percent.value ?? '—'}）` : '暂无置信度',
)
</script>

<template>
  <el-tooltip :content="tooltip" placement="top" :show-after="200">
    <span v-if="meta" class="conf" :class="`conf--${meta.tone}`">
      <span class="conf__dot" />
      {{ meta.text }}
      <span v-if="percent" class="conf__pct tnum">{{ percent }}</span>
    </span>
    <span v-else class="conf conf--unknown">
      <span class="conf__dot" />
      置信度未知
    </span>
  </el-tooltip>
</template>

<style scoped>
.conf {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 9px 2px 7px;
  font-size: var(--fs-xs);
  font-weight: 500;
  line-height: 1.6;
  white-space: nowrap;
  border: 1px solid;
  border-radius: var(--r-pill);
}

.conf__dot {
  width: 5px;
  height: 5px;
  background: currentcolor;
  border-radius: 50%;
}

.conf__pct {
  font-weight: 600;
  opacity: 0.7;
}

.conf--high {
  color: var(--ok-600);
  background: var(--ok-50);
  border-color: rgba(45, 212, 191, 0.3);
}

/* 中置信度用琥珀色：它要求用户采取额外动作（人工复核），必须比「高」更醒目 */
.conf--medium {
  color: var(--warn-600);
  background: var(--warn-50);
  border-color: rgba(251, 191, 36, 0.35);
}

.conf--low {
  color: var(--info-600);
  background: var(--info-50);
  border-color: rgba(139, 148, 158, 0.28);
}

.conf--unknown {
  color: var(--ink-400);
  background: var(--surface-1);
  border-color: var(--line-1);
}
</style>
