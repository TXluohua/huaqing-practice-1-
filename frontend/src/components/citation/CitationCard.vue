<script setup lang="ts">
/**
 * 引用卡片（FR-04 答案溯源，接口文档 §3.1）。
 *
 * 两种分支：
 *   图片类（source_type=image 或带 image_url）—— 显示缩略图，点击看大图；
 *   文字类 —— 显示「文档名 · 版本 · 章节 · 页码」+ 原文片段。
 *
 * 版本号（version）必须展示：P5「版本混乱」正是本系统的目标痛点之一，
 * 把 V3.2 和 V2.8 的结论混在一起用是现场事故的直接来源。
 *
 * 视觉上左侧留一条主色竖条：引用卡片在答案下方成列出现，
 * 竖条让它们一眼区别于正文，而不需要靠更重的边框。
 */
import { ArrowRight } from '@element-plus/icons-vue'
import { computed } from 'vue'

import type { Citation } from '@/api/chat'

const props = defineProps<{
  citation: Citation
  /** 列表中的序号，仅用于展示；不要拿它当 citation.id 用 */
  index?: number
  /** 片段最大展示字符数 */
  snippetLimit?: number
}>()

const emit = defineEmits<{ open: [citation: Citation] }>()

const isImage = computed(() => props.citation.source_type === 'image' || !!props.citation.image_url)

/** 出处单行摘要，形如「刻蚀设备维护手册 · V3.2 · §3.4 · P.118」 */
const sourceLine = computed(() => {
  const c = props.citation
  const parts = [
    c.doc,
    c.version ? `V${c.version.replace(/^v/i, '')}` : null,
    c.section ? `§${c.section}` : null,
    c.page ? `P.${c.page}` : null,
  ]
  return parts.filter(Boolean).join(' · ') || '未标注出处'
})

const sourceTypeText = computed(() => {
  switch (props.citation.source_type) {
    case 'ticket':
      return '历史工单'
    case 'image':
      return '图片依据'
    default:
      return '知识库文档'
  }
})

const snippet = computed(() => {
  const text = props.citation.snippet ?? ''
  const limit = props.snippetLimit ?? 100
  return text.length > limit ? `${text.slice(0, limit)}…` : text
})
</script>

<template>
  <div
    class="cite"
    :class="{ 'cite--image': isImage }"
    role="button"
    tabindex="0"
    @click="emit('open', citation)"
    @keydown.enter.prevent="emit('open', citation)"
    @keydown.space.prevent="emit('open', citation)"
  >
    <div class="cite__head">
      <span class="cite__badge tnum">{{ citation.id }}</span>
      <span class="cite__type" :class="{ 'cite__type--ticket': citation.source_type === 'ticket' }">
        {{ sourceTypeText }}
      </span>
      <span class="cite__source" :title="sourceLine">{{ sourceLine }}</span>
    </div>

    <!-- 图片分支：引用原图（/static/... dev 下由 Vite 代理到 :8000） -->
    <el-image
      v-if="isImage"
      class="cite__image"
      :src="citation.image_url ?? ''"
      fit="cover"
      :preview-src-list="[citation.image_url ?? '']"
      preview-teleported
      @click.stop
    >
      <template #error>
        <div class="cite__image-fallback">原图不可用</div>
      </template>
    </el-image>

    <!-- 文字分支：片段 + 「查看原文」入口 -->
    <p v-else-if="snippet" class="cite__snippet">{{ snippet }}</p>
    <p v-else class="cite__snippet cite__snippet--empty">（后端未返回原文片段）</p>

    <div class="cite__foot">
      <span v-if="citation.chunk_id" class="cite__chunk text-mono">{{ citation.chunk_id }}</span>
      <span class="cite__more">
        查看原文
        <el-icon><ArrowRight /></el-icon>
      </span>
    </div>
  </div>
</template>

<style scoped>
.cite {
  position: relative;
  padding: var(--sp-3) var(--sp-4) var(--sp-3) calc(var(--sp-4) + 4px);
  overflow: hidden;
  cursor: pointer;
  background: var(--surface-0);
  border: 1px solid var(--line-1);
  border-radius: var(--r-md);
  transition: border-color 0.16s var(--ease), box-shadow 0.16s var(--ease),
    transform 0.16s var(--ease);
}

/* 左侧主色竖条 */
.cite::before {
  position: absolute;
  top: 0;
  left: 0;
  width: 3px;
  height: 100%;
  content: '';
  background: var(--brand-200);
  transition: background 0.16s var(--ease);
}

.cite:hover {
  border-color: var(--brand-200);
  box-shadow: var(--sh-md);
  transform: translateY(-1px);
}

.cite:hover::before {
  background: var(--brand-600);
}

.cite:focus-visible {
  border-color: var(--brand-400);
  outline: none;
  box-shadow: 0 0 0 3px rgb(37 99 235 / 12%);
}

/* ------------------------------------------------------------------ 头部 */
.cite__head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: 7px;
}

/* 编号用实心方块而非 [n] 文本：与正文里的角标呼应，但更醒目 */
.cite__badge {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: center;
  min-width: 19px;
  height: 19px;
  padding: 0 5px;
  font-size: 11px;
  font-weight: 600;
  color: #fff;
  background: var(--brand-600);
  border-radius: 5px;
}

.cite__type {
  flex: 0 0 auto;
  padding: 1px 7px;
  font-size: 11px;
  font-weight: 500;
  color: var(--ink-500);
  background: var(--surface-2);
  border-radius: var(--r-pill);
}

.cite__type--ticket {
  color: var(--warn-600);
  background: var(--warn-50);
}

.cite__source {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  font-size: var(--fs-xs);
  color: var(--ink-500);
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ------------------------------------------------------------------ 正文 */
.cite__snippet {
  display: -webkit-box;
  margin: 0 0 var(--sp-2);
  overflow: hidden;
  font-size: var(--fs-sm);
  line-height: 1.65;
  color: var(--ink-700);
  word-break: break-word;
  /* 片段来自原文，保留换行更接近原貌；超出 3 行折叠，完整内容在抽屉里看 */
  white-space: pre-wrap;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
}

.cite__snippet--empty {
  color: var(--ink-400);
}

.cite__image {
  display: block;
  width: 100%;
  max-height: 168px;
  margin-bottom: var(--sp-2);
  overflow: hidden;
  border: 1px solid var(--line-1);
  border-radius: var(--r-sm);
}

.cite__image-fallback {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 84px;
  font-size: var(--fs-xs);
  color: var(--ink-400);
  background: var(--surface-1);
}

/* ------------------------------------------------------------------ 底部 */
.cite__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--sp-2);
}

.cite__chunk {
  overflow: hidden;
  color: var(--ink-300);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.cite__more {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 3px;
  font-size: var(--fs-xs);
  font-weight: 500;
  color: var(--brand-600);
  white-space: nowrap;
}

.cite__more .el-icon {
  transition: transform 0.16s var(--ease);
}

.cite:hover .cite__more .el-icon {
  transform: translateX(2px);
}
</style>
