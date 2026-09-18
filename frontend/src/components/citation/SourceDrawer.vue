<script setup lang="ts">
/**
 * 原文片段 / 原图查看抽屉（FR-04）。
 *
 * 刻意**不发请求**：Citation 里已经带了 snippet（接口文档 §3.1），
 * 点开就能看到原文，不需要再打一次「取切片详情」的接口。
 * 等 chunk 详情接口真做出来（§4.10 是按 doc 查，不是按 chunk_id 查），
 * 再在这里补一个「查看完整切片」按钮即可。
 */
import { computed } from 'vue'

import type { Citation } from '@/api/chat'

const props = defineProps<{
  modelValue: boolean
  citation: Citation | null
}>()

const emit = defineEmits<{ 'update:modelValue': [value: boolean] }>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const isImage = computed(() => !!props.citation?.image_url)

/** 元数据表：只展示后端真的给了的字段，空值不占位 */
const metaRows = computed(() => {
  const c = props.citation
  if (!c) return []
  return [
    { label: '引用编号', value: `[${c.id}]` },
    { label: '来源类型', value: c.source_type },
    { label: '文档', value: c.doc },
    { label: '版本', value: c.version },
    { label: '章节', value: c.section },
    { label: '页码', value: c.page === null ? null : `P.${c.page}` },
    { label: '切片 ID', value: c.chunk_id },
  ].filter((row) => row.value !== null && row.value !== undefined && row.value !== '')
})
</script>

<template>
  <el-drawer v-model="visible" title="引用原文" size="45%" :destroy-on-close="true">
    <div v-if="citation" class="source-drawer">
      <el-descriptions :column="1" size="small" border>
        <el-descriptions-item v-for="row in metaRows" :key="row.label" :label="row.label">
          {{ row.value }}
        </el-descriptions-item>
      </el-descriptions>

      <template v-if="isImage">
        <h4 class="source-drawer__title">原图</h4>
        <el-image
          class="source-drawer__image"
          :src="citation.image_url ?? ''"
          :preview-src-list="[citation.image_url ?? '']"
          fit="contain"
          preview-teleported
        >
          <template #error>
            <div class="source-drawer__fallback">
              原图不可用。dev 环境请确认 /static 代理已生效（见 vite.config.ts）。
            </div>
          </template>
        </el-image>
      </template>

      <template v-if="citation.snippet">
        <h4 class="source-drawer__title">原文片段</h4>
        <!-- pre-wrap：原文里的换行与缩进是判断参数表 / 步骤列表结构的关键信息 -->
        <div class="source-drawer__snippet">{{ citation.snippet }}</div>
      </template>

      <el-alert
        v-if="!citation.snippet && !isImage"
        type="info"
        :closable="false"
        show-icon
        title="后端未返回原文片段"
        description="Citation.snippet 为空，无法展示原文。请检查 verify 节点的引用回填逻辑。"
      />
    </div>

    <el-empty v-else description="未选择引用" />
  </el-drawer>
</template>

<style scoped>
.source-drawer__title {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin: var(--sp-6) 0 var(--sp-3);
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--ink-700);
  letter-spacing: 0.2px;
}

/* 小标题前加一段主色短线，抽屉内容多时更容易分段 */
.source-drawer__title::before {
  width: 3px;
  height: 13px;
  content: '';
  background: var(--brand-600);
  border-radius: 2px;
}

.source-drawer__image {
  width: 100%;
  max-height: 50vh;
  border: 1px solid var(--line-1);
  border-radius: var(--r-md);
}

.source-drawer__fallback {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 160px;
  padding: 0 var(--sp-4);
  font-size: var(--fs-sm);
  color: var(--ink-400);
  text-align: center;
  background: var(--surface-1);
}

/* 原文片段用比抽屉底（surface-0）更暗一档的 surface-1：
   它是「引来的外部材料」，压暗一档正好表达「这块不是我写的」 */
.source-drawer__snippet {
  padding: var(--sp-4);
  font-size: var(--fs-sm);
  line-height: 1.85;
  color: var(--ink-700);
  word-break: break-word;
  white-space: pre-wrap;
  background: var(--surface-1);
  border: 1px solid var(--line-1);
  border-left: 3px solid var(--brand-400);
  border-radius: var(--r-sm) var(--r-md) var(--r-md) var(--r-sm);
}
</style>
