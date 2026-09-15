<script setup lang="ts">
/**
 * 回答状态徽标（接口文档 §3.3）。
 *
 * 三种取值的语义差别很大，配色也必须区分开：
 *   OK          正常返回
 *   NOT_COVERED 知识库未覆盖 —— 这是**设计内的拒答**，不是故障。
 *               用灰色而非红色，避免用户把「系统如实说不知道」误当成报错。
 *   ERROR       链路异常 —— 这才是真故障，红色。
 *
 * 与 ConfidenceTag 用同一套 pill 样式，页脚一排才整齐。
 */
import { computed } from 'vue'

import type { Status } from '@/api/chat'

const props = defineProps<{ status: Status | null }>()

const meta = computed(() => {
  switch (props.status) {
    case 'OK':
      return { tone: 'ok', text: '正常回答', hint: '检索到的证据足以支撑结论' }
    case 'NOT_COVERED':
      return {
        tone: 'notcovered',
        text: '知识库未覆盖',
        hint: '未检索到可靠依据，系统拒答而非猜测（零幻觉策略）',
      }
    case 'ERROR':
      return { tone: 'error', text: '链路异常', hint: '问答链路出错，请查看后端日志 trace_id' }
    default:
      return null
  }
})
</script>

<template>
  <el-tooltip v-if="meta" :content="meta.hint" placement="top" :show-after="200">
    <span class="stat" :class="`stat--${meta.tone}`">
      <span class="stat__dot" />
      {{ meta.text }}
    </span>
  </el-tooltip>
</template>

<style scoped>
.stat {
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

.stat__dot {
  width: 5px;
  height: 5px;
  background: currentcolor;
  border-radius: 50%;
}

.stat--ok {
  color: var(--ok-600);
  background: var(--ok-50);
  border-color: #cbeed7;
}

/* 拒答用中性灰：它是「如实说不知道」，与红色的链路故障必须区分 */
.stat--notcovered {
  color: var(--info-600);
  background: var(--info-50);
  border-color: #e0e4ea;
}

.stat--error {
  color: var(--danger-600);
  background: var(--danger-50);
  border-color: #f7d4d1;
}
</style>
