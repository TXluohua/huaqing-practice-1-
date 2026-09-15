<script setup lang="ts">
/**
 * 图片缩略图。
 *
 * 三种用途共用：
 *   1. 输入区待发送的图片（可删除、可能仍在上传）；
 *   2. 用户消息里回显的图片；
 *   3. 展示 SSE image 事件的识别结果（image_type + 置信度）。
 *
 * recogn 只在第 3 种场景传入：识别发生在问答链路的 ingest_image 节点，
 * 不在上传接口（接口文档 §4.2），所以缩略图上传完成时还不知道识别结果。
 */
import { Loading } from '@element-plus/icons-vue'
import { computed } from 'vue'

import type { ImageEvent } from '@/api/chat'
import type { UploadedImage } from '@/stores/chat'

const props = defineProps<{
  image: UploadedImage
  /** SSE image 事件结果，未识别时为 null */
  recogn?: ImageEvent | null
  removable?: boolean
  size?: number
}>()

const emit = defineEmits<{ remove: [localId: string] }>()

const boxSize = computed(() => `${props.size ?? 80}px`)

/** 识别类型的中文说明；unknown 表示识别失败，链路会提示改用文字描述 */
const recognText = computed(() => {
  if (!props.recogn) return ''
  const type = props.recogn.image_type
  const percent = `${Math.round((props.recogn.confidence ?? 0) * 100)}%`
  if (!type || type === 'unknown') return `未识别 · ${percent}`
  return `${type} · ${percent}`
})

/** extracted 是自由 dict（后端 dict[str, Any]），渲染成 key: value 芯片 */
const extractedChips = computed(() => {
  const data = props.recogn?.extracted
  if (!data) return []
  return Object.entries(data).map(([key, value]) => ({
    key,
    value: typeof value === 'string' ? value : JSON.stringify(value),
  }))
})
</script>

<template>
  <div class="image-thumb" :style="{ width: boxSize }">
    <el-image
      class="image-thumb__img"
      :style="{ width: boxSize, height: boxSize }"
      :src="image.previewUrl"
      fit="cover"
      :preview-src-list="[image.previewUrl]"
      preview-teleported
    >
      <template #error>
        <div class="image-thumb__error">图片不可读</div>
      </template>
    </el-image>

    <!-- 上传中：半透明遮罩 + 加载圈 -->
    <div v-if="image.uploading" class="image-thumb__mask">
      <el-icon class="is-loading"><Loading /></el-icon>
    </div>

    <!-- 上传失败 -->
    <el-tooltip v-else-if="image.error" :content="image.error" placement="top">
      <div class="image-thumb__mask image-thumb__mask--error">上传失败</div>
    </el-tooltip>

    <button
      v-if="removable"
      class="image-thumb__remove"
      type="button"
      title="移除这张图片"
      @click.stop="emit('remove', image.localId)"
    >
      ×
    </button>

    <div v-if="recogn" class="image-thumb__recogn">
      <span
        class="image-thumb__recogn-tag"
        :class="
          recogn.image_type && recogn.image_type !== 'unknown'
            ? 'image-thumb__recogn-tag--ok'
            : 'image-thumb__recogn-tag--warn'
        "
      >
        {{ recognText }}
      </span>
      <div v-if="extractedChips.length" class="image-thumb__chips">
        <span v-for="chip in extractedChips" :key="chip.key" class="image-thumb__chip">
          {{ chip.key }}: {{ chip.value }}
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.image-thumb {
  position: relative;
}

.image-thumb__img {
  display: block;
  overflow: hidden;
  border: 1px solid var(--line-1);
  border-radius: var(--r-md);
}

.image-thumb__error {
  display: flex;
  align-items: center;
  justify-content: center;
  height: 100%;
  font-size: var(--fs-xs);
  color: var(--ink-400);
  background: var(--surface-1);
}

.image-thumb__mask {
  position: absolute;
  top: 0;
  left: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  height: 100%;
  font-size: var(--fs-xs);
  color: #fff;
  background: rgb(16 24 40 / 50%);
  border-radius: var(--r-md);
}

.image-thumb__mask--error {
  color: #ffd7d7;
  cursor: help;
}

/* 移除按钮压在右上角，用白描边与图片内容分离，避免看不清 */
.image-thumb__remove {
  position: absolute;
  top: -6px;
  right: -6px;
  display: flex;
  align-items: center;
  justify-content: center;
  width: 19px;
  height: 19px;
  padding: 0;
  font-family: inherit;
  font-size: 14px;
  line-height: 1;
  color: var(--ink-500);
  cursor: pointer;
  background: var(--surface-0);
  border: 1px solid var(--line-2);
  border-radius: 50%;
  box-shadow: var(--sh-xs);
  transition: color 0.15s var(--ease), background 0.15s var(--ease),
    border-color 0.15s var(--ease);
}

.image-thumb__remove:hover {
  color: #fff;
  background: var(--danger-600);
  border-color: var(--danger-600);
}

.image-thumb__recogn {
  margin-top: 5px;
}

.image-thumb__recogn-tag {
  display: inline-block;
  padding: 1px 7px;
  font-size: 11px;
  font-weight: 500;
  border-radius: var(--r-pill);
}

.image-thumb__recogn-tag--ok {
  color: var(--ok-600);
  background: var(--ok-50);
}

.image-thumb__recogn-tag--warn {
  color: var(--warn-600);
  background: var(--warn-50);
}

.image-thumb__chips {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 4px;
}

.image-thumb__chip {
  padding: 1px 6px;
  font-size: 11px;
  color: var(--ink-500);
  background: var(--surface-2);
  border-radius: 4px;
}
</style>
