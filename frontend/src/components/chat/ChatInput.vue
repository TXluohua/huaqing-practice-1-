<script setup lang="ts">
/**
 * 输入区：文字、图片上传、粘贴截图、检索条件、停止生成。
 *
 * 几个契约要点：
 *
 *   - **图片走两段式**（接口文档 §4.2/§7）：选中即上传拿 image_id，
 *     提问时只在 image_ids 里带 ID。SSE 端点本身不处理文件上传。
 *   - **粘贴截图**（FR-02 的现场场景）：`paste` 事件里读 clipboardData.files。
 *     必须 preventDefault，否则浏览器会把图片粘贴成 base64 塞进输入框。
 *   - **培训模式**是接口文档 §8 的缺口①（state.py 没有该字段），
 *     照发但标注「实验性」，后端拒绝时按普通错误提示，不影响问答主链路。
 *   - **快捷提问**（FR-09 备件查询）：后端 `parts_query` 已接进链路，但三个视图都没有入口，
 *     用户不知道「备件/替代件」也能问。这里给几个模板按钮，点一下把问题填进输入框并聚焦，
 *     **不直接发送** —— 模板里的部件名/型号要用户自己替换，直接发出去只会得到拒答。
 *
 * 视觉上整个输入区是一张卡片：工具栏、文本框、按钮都在同一张卡里，
 * 而不是「文本框 + 一排按钮」拼起来。所以 textarea 自身不要边框，
 * 由卡片承担聚焦态的高亮。
 */
import { Filter, Picture, Promotion, VideoPause } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { computed, onBeforeUnmount, ref } from 'vue'

import { IMAGE_MAX_COUNT, QUESTION_MAX_LEN } from '@/api/chat'
import { humanizeError } from '@/api/http'
import { IMAGE_LIMITS } from '@/api/upload'
import ImageThumb from '@/components/chat/ImageThumb.vue'
import { useChatStore } from '@/stores/chat'

const chatStore = useChatStore()

const text = ref('')
const showFilters = ref(false)
/** el-upload 需要 ref 才能手动触发选择文件 */
const uploadRef = ref()
/** el-input 的 ref，快捷提问填充后要把光标送进去 */
const inputRef = ref<{ focus: () => void } | null>(null)

/**
 * 快捷提问模板（FR-09）。后端 parts_query 已接进链路，问题在于入口不可见。
 * 模板故意留 `<部件名>` / `<型号>` 占位符：备件问题的答案依赖台账里的具体件号，
 * 拿一个不存在的件号去问只会触发 fail-closed 拒答，反而误导用户。
 */
const QUICK_ASKS: { label: string; text: string }[] = [
  {
    label: '备件库存',
    text: '腔体门 O-ring 还有库存吗？有没有替代件、多久到货？',
  },
  {
    label: '替代件',
    text: '<部件名> 缺货时可以用什么材料替代？有哪些限制和兼容性依据？',
  },
  {
    label: '参数查询',
    text: 'CVD 沉积温度的工艺窗口是多少？超窗口会有什么后果？',
  },
]
/** 卡片聚焦态由自己维护：textarea 的 focus 事件在 EP 里被包了一层 */
const focused = ref(false)

const accept = computed(() => IMAGE_LIMITS.mime.join(','))

const charCount = computed(() => text.value.length)
const overLimit = computed(() => charCount.value > QUESTION_MAX_LEN)
const hasUploading = computed(() => chatStore.pendingImages.some((img) => img.uploading))
const canSend = computed(
  () => !!text.value.trim() && !overLimit.value && !chatStore.streaming && !hasUploading.value,
)

/** 接近上限时才显示计数，平时不占视觉噪音 */
const showCount = computed(() => charCount.value > QUESTION_MAX_LEN * 0.6)

/** 发送：清空输入框的时机在 send 成功入队之后，失败时保留用户输入 */
async function submit(): Promise<void> {
  if (!canSend.value) return
  const question = text.value
  try {
    await chatStore.send(question)
    text.value = ''
  } catch (error) {
    ElMessage.error(humanizeError(error))
  }
}

/**
 * 快捷提问：填进输入框并聚焦，**不自动发送**。
 * 已有草稿时不覆盖 —— 用户可能写了一半，点模板只是想参考措辞；
 * 覆盖用户输入是不可逆的，而追加又会让两种意图混在一句里。
 */
function useQuickAsk(item: { text: string }): void {
  if (text.value.trim() && text.value.trim() !== item.text) {
    ElMessage.info('输入框已有内容，快捷提问未覆盖，请先清空或手动编辑。')
    inputRef.value?.focus()
    return
  }
  text.value = item.text
  inputRef.value?.focus()
}

/** Enter 发送、Shift+Enter 换行（中文输入法组合态下不触发） */
function onKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter' || event.shiftKey) return
  if (event.isComposing) return
  event.preventDefault()
  void submit()
}

async function addFile(file: File): Promise<void> {
  try {
    await chatStore.addImage(file)
  } catch (error) {
    ElMessage.error(humanizeError(error))
  }
}

/** 粘贴截图：FR-02 的主要入口，工程师习惯直接复制告警画面 */
function onPaste(event: ClipboardEvent): void {
  const files = Array.from(event.clipboardData?.files ?? []).filter((file) =>
    file.type.startsWith('image/'),
  )
  if (files.length === 0) return
  // 阻止浏览器把图片转成 base64 插进 textarea
  event.preventDefault()
  for (const file of files) void addFile(file)
}

function onSelectFile(file: { raw?: File }): boolean {
  if (file.raw) void addFile(file.raw)
  return false
}

function onRemove(localId: string): void {
  chatStore.removeImage(localId)
}

onBeforeUnmount(() => {
  // 输入区里尚未发送的图片 blob URL 需要释放
  chatStore.clearImages()
})
</script>

<template>
  <div class="composer">
    <div class="composer__wrap">
      <!-- 待发送的图片 -->
      <div v-if="chatStore.pendingImages.length" class="composer__images">
        <ImageThumb
          v-for="img in chatStore.pendingImages"
          :key="img.localId"
          :image="img"
          removable
          :size="62"
          @remove="onRemove"
        />
      </div>

      <!-- 检索条件（接口文档 §4.3：device_model 走元数据过滤，category 走分类过滤） -->
      <transition name="filters">
        <div v-if="showFilters" class="composer__filters">
          <el-input
            v-model="chatStore.deviceModel"
            size="small"
            placeholder="设备型号，如 Etcher-A（用于元数据过滤）"
            clearable
          />
          <el-select v-model="chatStore.category" size="small" placeholder="分类" clearable>
            <el-option label="设备维护" value="设备维护" />
            <el-option label="工艺" value="工艺" />
            <el-option label="标准" value="标准" />
          </el-select>
        </div>
      </transition>

      <!-- 快捷提问：让「备件 / 替代件 / 参数」这类能力可见（FR-09） -->
      <div class="composer__quick">
        <span class="composer__quick-label">快捷提问</span>
        <button
          v-for="item in QUICK_ASKS"
          :key="item.label"
          class="composer__quick-btn"
          type="button"
          :disabled="chatStore.streaming"
          @click="useQuickAsk(item)"
        >
          {{ item.label }}
        </button>
      </div>

      <!-- 输入卡片 -->
      <div class="composer__card" :class="{ 'composer__card--focus': focused }">
        <el-input
          ref="inputRef"
          v-model="text"
          class="composer__textarea"
          type="textarea"
          :rows="2"
          :autosize="{ minRows: 2, maxRows: 8 }"
          resize="none"
          :placeholder="`描述现象或提问，可粘贴截图（Enter 发送，Shift+Enter 换行）`"
          @keydown="onKeydown"
          @paste="onPaste"
          @focus="focused = true"
          @blur="focused = false"
        />

        <div class="composer__bar">
          <div class="composer__bar-left">
            <el-upload
              ref="uploadRef"
              :accept="accept"
              :show-file-list="false"
              :auto-upload="false"
              :on-change="onSelectFile"
            >
              <el-tooltip
                :content="
                  chatStore.canAttachMore
                    ? `添加图片（最多 ${IMAGE_MAX_COUNT} 张，单张 ≤ ${IMAGE_LIMITS.maxMb}MB）`
                    : `最多 ${IMAGE_MAX_COUNT} 张`
                "
                placement="top"
                :show-after="300"
              >
                <button
                  class="composer__icon-btn"
                  type="button"
                  :disabled="!chatStore.canAttachMore"
                >
                  <el-icon><Picture /></el-icon>
                  图片
                </button>
              </el-tooltip>
            </el-upload>

            <button
              class="composer__icon-btn"
              :class="{ 'composer__icon-btn--on': showFilters }"
              type="button"
              @click="showFilters = !showFilters"
            >
              <el-icon><Filter /></el-icon>
              检索条件
            </button>

            <el-radio-group v-model="chatStore.mode" size="small" class="composer__mode">
              <el-radio-button value="qa">问答</el-radio-button>
              <el-radio-button value="training">培训讲解</el-radio-button>
            </el-radio-group>
          </div>

          <div class="composer__bar-right">
            <span
              v-if="showCount || overLimit"
              class="composer__count tnum"
              :class="{ 'composer__count--over': overLimit }"
            >
              {{ charCount }} / {{ QUESTION_MAX_LEN }}
            </span>

            <!-- 培训模式是接口文档 §8 缺口①，标注实验性以免被当成稳定能力 -->
            <span v-if="chatStore.mode === 'training'" class="composer__badge">实验性</span>

            <el-button
              v-if="chatStore.streaming"
              size="small"
              :icon="VideoPause"
              @click="chatStore.stop()"
            >
              停止
            </el-button>
            <el-button
              v-else
              class="composer__send"
              type="primary"
              :icon="Promotion"
              :disabled="!canSend"
              @click="submit"
            >
              发送
            </el-button>
          </div>
        </div>
      </div>

      <!-- 状态提示：上传中 / 超长。两者互斥，同时只出一条 -->
      <p v-if="hasUploading" class="composer__tip">
        <span class="composer__spinner" />
        图片上传中，请稍候…
      </p>
      <p v-else-if="overLimit" class="composer__tip composer__tip--error">
        已超过 {{ QUESTION_MAX_LEN }} 字上限，请精简后再发送。
      </p>
    </div>
  </div>
</template>

<style scoped>
.composer {
  flex: 0 0 auto;
  background: var(--surface-1);
}

/* 与消息列同宽，输入框和上方的对话内容左右对齐 */
.composer__wrap {
  width: 100%;
  max-width: var(--conversation-w);
  padding: 0 var(--sp-5) var(--sp-5);
  margin: 0 auto;
}

.composer__images {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  padding: var(--sp-3);
  margin-bottom: var(--sp-2);
  background: var(--surface-0);
  border: 1px solid var(--line-1);
  border-radius: var(--r-md);
}

/* 快捷提问：pill 描边按钮，视觉重量低于发送按钮，避免和主行动抢注意力 */
.composer__quick {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  align-items: center;
  margin-bottom: var(--sp-2);
  padding: 0 2px;
}

.composer__quick-label {
  font-size: var(--fs-xs);
  color: var(--ink-400);
}

.composer__quick-btn {
  padding: 3px 11px;
  font-family: inherit;
  font-size: var(--fs-xs);
  color: var(--ink-500);
  cursor: pointer;
  background: var(--surface-0);
  border: 1px solid var(--line-1);
  border-radius: var(--r-pill);
  transition: color 0.15s var(--ease), border-color 0.15s var(--ease),
    background 0.15s var(--ease);
}

.composer__quick-btn:hover:not(:disabled) {
  color: var(--brand-600);
  background: var(--brand-50);
  border-color: var(--brand-100);
}

.composer__quick-btn:disabled {
  color: var(--ink-300);
  cursor: not-allowed;
}

.composer__filters {
  display: flex;
  gap: var(--sp-2);
  padding: var(--sp-3);
  margin-bottom: var(--sp-2);
  background: var(--surface-0);
  border: 1px solid var(--line-1);
  border-radius: var(--r-md);
}

/* ------------------------------------------------------------------ 卡片 */
/* 深色玻璃底 + 内阴影：内阴影让输入框看起来是「凹进面板的一块屏幕」，
   与凸起的按钮形成方向对比，这是仪表盘上常见的物理隐喻 */
.composer__card {
  padding: var(--sp-2) var(--sp-3) var(--sp-2);
  background: rgba(22, 27, 34, 0.72);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(0, 212, 255, 0.16);
  border-radius: var(--r-lg);
  box-shadow: inset 0 1px 0 rgba(0, 0, 0, 0.35), var(--sh-md);
  transition: border-color 0.18s var(--ease), box-shadow 0.18s var(--ease);
}

/* 聚焦时整卡高亮，而不是给 textarea 画一圈内边框 */
.composer__card--focus {
  border-color: rgba(0, 212, 255, 0.55);
  box-shadow: inset 0 1px 0 rgba(0, 0, 0, 0.35), var(--sh-md), var(--glow-2);
}

/* 去掉 EP textarea 自带的边框与内阴影，让卡片成为唯一视觉边界 */
.composer__textarea :deep(.el-textarea__inner) {
  padding: var(--sp-2) var(--sp-2) 0;
  font-family: inherit;
  font-size: var(--fs-md);
  line-height: 1.7;
  color: var(--ink-900);
  background: transparent;
  border: none;
  box-shadow: none;
}

.composer__textarea :deep(.el-textarea__inner::placeholder) {
  color: var(--ink-300);
}

/* ------------------------------------------------------------------ 工具栏 */
.composer__bar {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  align-items: center;
  justify-content: space-between;
  padding-top: var(--sp-2);
  margin-top: var(--sp-2);
  border-top: 1px solid var(--line-1);
}

.composer__bar-left,
.composer__bar-right {
  display: flex;
  gap: var(--sp-2);
  align-items: center;
}

/* 文字按钮而非 EP 的按钮：工具栏里三个控件都是低频操作，不该抢主按钮的注意力 */
.composer__icon-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 10px;
  font-family: inherit;
  font-size: var(--fs-xs);
  color: var(--ink-500);
  cursor: pointer;
  background: transparent;
  border: 1px solid transparent;
  border-radius: var(--r-sm);
  transition: color 0.15s var(--ease), background 0.15s var(--ease);
}

.composer__icon-btn:hover:not(:disabled) {
  color: var(--brand-600);
  background: var(--brand-50);
}

.composer__icon-btn--on {
  color: var(--brand-600);
  background: var(--brand-50);
}

.composer__icon-btn:disabled {
  color: var(--ink-300);
  cursor: not-allowed;
}

/* el-upload 会包一层 div，让它与相邻按钮的基线对齐 */
.composer__bar-left :deep(.el-upload) {
  display: inline-flex;
}

.composer__mode :deep(.el-radio-button__inner) {
  padding: 5px 12px;
  font-size: var(--fs-xs);
}

.composer__count {
  font-size: var(--fs-xs);
  color: var(--ink-400);
}

.composer__count--over {
  font-weight: 600;
  color: var(--danger-600);
}

/* 发送按钮：整页唯一一处实心青蓝，pill 形。
   刻意压掉 EP 的 --el-button-* 背景 —— 这个按钮是主行动，用渐变实底而不是
   24% 透明度的 plain 样式，扫视时才能立刻定位到「回车提交」这个动作。
   disabled 态必须显式写：本类的选择器带 scoped 属性，特异性高于
   .el-button.is-disabled，不写就会让禁用按钮也保持亮青色。 */
.composer__send {
  border: none;
  border-radius: var(--r-pill);
  background-image: linear-gradient(140deg, #00d4ff, #0066ff);
  box-shadow: 0 2px 10px rgba(0, 212, 255, 0.25);
}

.composer__send:hover:not(.is-disabled) {
  background-image: linear-gradient(140deg, #33ddff, #1a7bff);
  box-shadow: 0 2px 14px rgba(0, 212, 255, 0.4);
}

.composer__send.is-disabled {
  color: var(--ink-300);
  background-image: none;
  background-color: var(--surface-3);
  box-shadow: none;
}

.composer__badge {
  padding: 1px 7px;
  font-size: 11px;
  font-weight: 500;
  color: var(--warn-600);
  background: var(--warn-50);
  border-radius: var(--r-pill);
}

/* ------------------------------------------------------------------ 提示 */
.composer__tip {
  display: flex;
  align-items: center;
  gap: 6px;
  margin: 8px 2px 0;
  font-size: var(--fs-xs);
  color: var(--ink-400);
}

.composer__tip--error {
  color: var(--danger-600);
}

.composer__spinner {
  width: 11px;
  height: 11px;
  border: 1.6px solid var(--brand-100);
  border-top-color: var(--brand-600);
  border-radius: 50%;
  animation: composer-spin 0.7s linear infinite;
}

@keyframes composer-spin {
  to {
    transform: rotate(360deg);
  }
}

/* ------------------------------------------------------------ 过渡动画 */
.filters-enter-active,
.filters-leave-active {
  transition: opacity 0.18s var(--ease), transform 0.18s var(--ease);
}
.filters-enter-from,
.filters-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

@media (max-width: 720px) {
  .composer__wrap {
    padding-right: var(--sp-4);
    padding-left: var(--sp-4);
  }
  /* 窄屏下模式切换换行到第二排，避免把发送按钮挤出去 */
  .composer__bar {
    align-items: flex-end;
  }
}
</style>
