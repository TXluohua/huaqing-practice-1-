<script setup lang="ts">
/**
 * 管理页（FR-08）：健康检查、文档入库、索引重建、任务进度、切片查询。
 *
 * 除以之外还放了「身份设置」：接口文档 §1.3 规定 X-User-Id / X-User-Role 由请求头透传，
 * 本期不做登录。把它放在管理页而不是全局设置里，是为了让「切角色」这个动作
 * 看起来就是个调试开关，而不是一个像模像样的账号体系。
 *
 * 轮询注意：任务轮询在组件卸载时必须清理，否则离开页面后仍会持续打接口。
 *
 * 布局：不用 el-card，改用自绘的 .panel。EP 卡片的头部内边距与边框偏重，
 * 一页摞五张会显得很「后台系统」；自绘面板让标题、说明、操作按钮的层级更清晰。
 */
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'

import {
  COMPONENT_LABELS,
  DOC_ACCEPT,
  DOC_CATEGORIES,
  getJob,
  health,
  listChunks,
  rebuildIndex,
  uploadDocument,
  type ChunkItem,
  type HealthResponse,
  type JobStatusResponse,
} from '@/api/admin'
import {
  getUserId,
  getUserRole,
  humanizeError,
  isApiError,
  setUserId,
  setUserRole,
  type ApiError,
  type UserRole,
} from '@/api/http'

// ------------------------------------------------------------------ 健康检查

const healthData = ref<HealthResponse | null>(null)
const healthError = ref<ApiError | null>(null)
const healthLoading = ref(false)

/** 组件级"降级提示"：MCP 加载失败属 R7 场景，主链路仍可用，不该整页报红 */
const degradedComponents = ref<string[]>([])

async function refreshHealth(): Promise<void> {
  healthLoading.value = true
  healthError.value = null
  try {
    const data = await health()
    healthData.value = data
    degradedComponents.value = Object.entries(data.components)
      .filter(([, item]) => !item.ok)
      .map(([name]) => COMPONENT_LABELS[name] ?? name)
  } catch (error) {
    healthData.value = null
    degradedComponents.value = []
    healthError.value = isApiError(error) ? error : null
  } finally {
    healthLoading.value = false
  }
}

/** 整体状态色调：ok 绿 / degraded 琥珀 / 其余红 */
function healthTone(status: string): string {
  if (status === 'ok') return 'ok'
  return status === 'degraded' ? 'warn' : 'error'
}

// ------------------------------------------------------------------ 身份设置

const identity = reactive({
  userId: getUserId(),
  role: getUserRole() as UserRole,
})

function saveIdentity(): void {
  setUserId(identity.userId)
  setUserRole(identity.role)
  ElMessage.success('身份已保存，后续请求将携带新的 X-User-Id / X-User-Role')
}

// -------------------------------------------------------------------- 文档入库

const docForm = reactive({
  docTitle: '',
  version: '',
  category: DOC_CATEGORIES[0] as string,
  uploader: '',
})
const docFile = ref<File | null>(null)
const uploading = ref(false)
const docJobId = ref<string | null>(null)

function onDocFileChange(file: { raw?: File }): boolean {
  docFile.value = file.raw ?? null
  // 标题默认取文件名，减少一次手工输入
  if (docFile.value && !docForm.docTitle) {
    docForm.docTitle = docFile.value.name.replace(/\.[^.]+$/, '')
  }
  return false
}

async function submitDocument(): Promise<void> {
  if (!docFile.value) {
    ElMessage.warning('请先选择文件。')
    return
  }
  if (!docForm.docTitle.trim() || !docForm.version.trim() || !docForm.category.trim()) {
    ElMessage.warning('文档标题、版本、分类均为必填（元数据完整率 100% 要求 page + version 齐备）。')
    return
  }

  uploading.value = true
  try {
    const formData = new FormData()
    formData.append('file', docFile.value, docFile.value.name)
    formData.append('doc_title', docForm.docTitle.trim())
    formData.append('version', docForm.version.trim())
    formData.append('category', docForm.category)
    if (docForm.uploader.trim()) formData.append('uploader', docForm.uploader.trim())

    const response = await uploadDocument(formData)
    docJobId.value = response.job_id
    ElMessage.success(`已提交入库，doc_id=${response.doc_id}`)
    startPolling(response.job_id)
  } catch (error) {
    ElMessage.error(humanizeError(error))
  } finally {
    uploading.value = false
  }
}

// ------------------------------------------------------------------ 索引重建

const rebuildScope = ref<'all' | 'incremental'>('all')
const rebuildForce = ref(false)
const rebuilding = ref(false)

async function doRebuild(): Promise<void> {
  rebuilding.value = true
  try {
    const response = await rebuildIndex({ scope: rebuildScope.value, force: rebuildForce.value })
    ElMessage.success(`已触发重建，job_id=${response.job_id}（验收标准 ≤ 5 分钟）`)
    startPolling(response.job_id)
  } catch (error) {
    ElMessage.error(humanizeError(error))
  } finally {
    rebuilding.value = false
  }
}

// ------------------------------------------------------------------ 任务轮询

const job = ref<JobStatusResponse | null>(null)
let pollTimer: number | undefined

function stopPolling(): void {
  if (pollTimer !== undefined) {
    window.clearInterval(pollTimer)
    pollTimer = undefined
  }
}

/**
 * 按 job_id 轮询进度（接口文档 §4.9）。
 * 轮询间隔 1.5s：入库任务量级在分钟级，更密没有意义，反而刷后端日志。
 */
function startPolling(jobId: string): void {
  stopPolling()
  job.value = null

  const tick = async (): Promise<void> => {
    try {
      const data = await getJob(jobId)
      job.value = data
      if (data.status === 'succeeded' || data.status === 'failed') {
        stopPolling()
        if (data.status === 'succeeded') ElMessage.success('任务完成')
        else ElMessage.error(`任务失败：${data.message}`)
      }
    } catch (error) {
      // 任务不存在（404）或后端掉了：停止轮询，避免无限打请求
      stopPolling()
      ElMessage.error(humanizeError(error))
    }
  }

  void tick()
  pollTimer = window.setInterval(() => void tick(), 1500)
}

// ------------------------------------------------------------------ 切片查询

const chunkQuery = reactive({
  q: '',
  docId: undefined as number | undefined,
  page: undefined as number | undefined,
})
const chunks = ref<ChunkItem[]>([])
const chunkTotal = ref(0)
const chunkLoading = ref(false)
const chunkError = ref<ApiError | null>(null)
const chunkLimit = ref(20)
const chunkOffset = ref(0)

async function searchChunks(): Promise<void> {
  chunkLoading.value = true
  chunkError.value = null
  try {
    const response = await listChunks({
      q: chunkQuery.q.trim() || undefined,
      doc_id: chunkQuery.docId,
      page: chunkQuery.page,
      limit: chunkLimit.value,
      offset: chunkOffset.value,
    })
    chunks.value = response.items
    chunkTotal.value = response.total
  } catch (error) {
    chunks.value = []
    chunkTotal.value = 0
    chunkError.value = isApiError(error) ? error : null
  } finally {
    chunkLoading.value = false
  }
}

function onChunkPage(target: number): void {
  chunkOffset.value = (target - 1) * chunkLimit.value
  void searchChunks()
}

onMounted(() => void refreshHealth())
// 关键：离开页面必须停掉轮询，否则会一直打后端
onBeforeUnmount(stopPolling)
</script>

<template>
  <div class="admin">
    <div class="admin__inner">
      <header class="admin__page-head">
        <h1 class="admin__page-title">管理</h1>
        <p class="admin__page-desc">
          系统健康、知识库入库与索引维护、切片检索调试。所有操作直接作用于后端服务。
        </p>
      </header>

      <!-- ---------------------------------------------------------- 健康检查 -->
      <section class="panel">
        <div class="panel__head">
          <div class="panel__head-text">
            <h2 class="panel__title">系统健康检查</h2>
            <p class="panel__desc">NFR-06：任一关键组件不可用时给出明确降级提示</p>
          </div>
          <el-button size="small" :icon="Refresh" :loading="healthLoading" @click="refreshHealth">
            刷新
          </el-button>
        </div>

        <div class="panel__body">
          <div v-if="healthError" class="note note--error">
            <p class="note__title">健康检查失败：{{ healthError.code }}</p>
            <p class="note__text">{{ humanizeError(healthError) }}</p>
          </div>

          <template v-else-if="healthData">
            <div class="health-head">
              <span class="health-badge" :class="`health-badge--${healthTone(healthData.status)}`">
                <span class="health-badge__dot" />
                {{ healthData.status }}
              </span>
              <span class="health-meta">版本 {{ healthData.version }}</span>
              <span class="health-meta text-mono">trace: {{ healthData.trace_id }}</span>
            </div>

            <!-- MCP / 检查点等非关键依赖失败时的降级提示（开发文档 R7：不得中断主链路） -->
            <div v-if="degradedComponents.length" class="note note--warn">
              <p class="note__title">部分组件降级</p>
              <p class="note__text">
                以下组件不可用，主链路仍可用：{{ degradedComponents.join('、') }}
              </p>
            </div>

            <!--
              组件清单做成卡片网格而不是表格行：每张卡带一条状态色顶边线 + LED 灯，
              七八个组件平铺一屏，哪个挂了不用逐行读文字就能看出来。
            -->
            <ul class="components">
              <li
                v-for="(item, name) in healthData.components"
                :key="name"
                class="components__card"
                :class="item.ok ? 'components__card--ok' : 'components__card--bad'"
              >
                <div class="components__head">
                  <!-- 只有故障灯不闪：七八个绿灯一起脉冲会变成一屏噪声，
                       而且「常亮」本身就是正常的语义 -->
                  <span
                    class="led"
                    :class="item.ok ? 'led--ok' : 'led--danger led--pulse'"
                    aria-hidden="true"
                  />
                  <span class="components__name">{{ COMPONENT_LABELS[name] ?? name }}</span>
                  <span class="components__tone">{{ item.ok ? 'OK' : 'DOWN' }}</span>
                </div>
                <div class="components__detail text-mono">{{ item.detail ?? '—' }}</div>
              </li>
            </ul>
          </template>

          <el-skeleton v-else :rows="4" animated />
        </div>
      </section>

      <!-- ------------------------------------------------------------ 身份 -->
      <section class="panel">
        <div class="panel__head">
          <div class="panel__head-text">
            <h2 class="panel__title">请求身份</h2>
            <p class="panel__desc">
              接口文档 §1.3 透传，本期不做登录。角色会被透传到检索层做权限过滤（检索即鉴权），
              本页与前端不做任何答案裁剪。
            </p>
          </div>
        </div>

        <div class="panel__body">
          <div class="row">
            <el-input
              v-model="identity.userId"
              size="small"
              class="row__field"
              placeholder="X-User-Id，缺省 anonymous"
            />
            <el-select v-model="identity.role" size="small" class="row__field--sm">
              <el-option label="engineer" value="engineer" />
              <el-option label="admin" value="admin" />
            </el-select>
            <el-button size="small" type="primary" @click="saveIdentity">保存</el-button>
          </div>
        </div>
      </section>

      <!-- ---------------------------------------------------------- 文档入库 -->
      <section class="panel">
        <div class="panel__head">
          <div class="panel__head-text">
            <h2 class="panel__title">文档入库</h2>
            <p class="panel__desc">FR-08：异步解析、分块并写入向量库，提交后可在任务进度中查看</p>
          </div>
        </div>

        <div class="panel__body">
          <el-form label-width="88px" size="small" class="doc-form">
            <el-form-item label="文档文件">
              <el-upload
                :accept="DOC_ACCEPT"
                :show-file-list="true"
                :auto-upload="false"
                :limit="1"
                :on-change="onDocFileChange"
              >
                <el-button size="small">选择文件</el-button>
                <template #tip>
                  <div class="hint">支持 PDF / MD / TXT / DOCX / PPTX / XLSX</div>
                </template>
              </el-upload>
            </el-form-item>

            <div class="doc-form__grid">
              <el-form-item label="文档标题">
                <el-input v-model="docForm.docTitle" placeholder="如：刻蚀设备维护手册" />
              </el-form-item>
              <el-form-item label="版本">
                <el-input v-model="docForm.version" placeholder="必填，如 V3.2" />
              </el-form-item>
              <el-form-item label="分类">
                <el-select v-model="docForm.category">
                  <el-option v-for="item in DOC_CATEGORIES" :key="item" :label="item" :value="item" />
                </el-select>
              </el-form-item>
              <el-form-item label="上传人">
                <el-input v-model="docForm.uploader" placeholder="选填" />
              </el-form-item>
            </div>

            <el-form-item>
              <el-button type="primary" :loading="uploading" @click="submitDocument">
                提交入库（异步）
              </el-button>
            </el-form-item>
          </el-form>
        </div>
      </section>

      <!-- ---------------------------------------------------------- 索引重建 -->
      <section class="panel">
        <div class="panel__head">
          <div class="panel__head-text">
            <h2 class="panel__title">索引重建</h2>
            <p class="panel__desc">接口文档 §4.8，验收标准：全量重建 ≤ 5 分钟</p>
          </div>
        </div>

        <div class="panel__body">
          <div class="row">
            <el-radio-group v-model="rebuildScope" size="small">
              <el-radio-button value="all">全量</el-radio-button>
              <el-radio-button value="incremental">增量</el-radio-button>
            </el-radio-group>
            <el-checkbox v-model="rebuildForce" size="small">强制重建（忽略切片版本）</el-checkbox>
            <el-button type="primary" size="small" :loading="rebuilding" @click="doRebuild">
              一键重建索引
            </el-button>
          </div>
        </div>
      </section>

      <!-- ---------------------------------------------------------- 任务进度 -->
      <section v-if="job" class="panel">
        <div class="panel__head">
          <div class="panel__head-text">
            <h2 class="panel__title">任务进度</h2>
            <p class="panel__desc text-mono">{{ job.job_id }}</p>
          </div>
          <span
            class="health-badge"
            :class="
              job.status === 'succeeded'
                ? 'health-badge--ok'
                : job.status === 'failed'
                  ? 'health-badge--error'
                  : 'health-badge--warn'
            "
          >
            <span class="health-badge__dot" />
            {{ job.status }}
          </span>
        </div>

        <div class="panel__body">
          <el-progress
            :percentage="Math.round((job.progress ?? 0) * 100)"
            :stroke-width="10"
            :show-text="true"
          />
          <p class="hint">{{ job.message || '—' }}</p>
          <details v-if="job.result" class="job-result">
            <summary>查看原始结果</summary>
            <pre class="job-result__pre">{{ JSON.stringify(job.result, null, 2) }}</pre>
          </details>
        </div>
      </section>

      <!-- ---------------------------------------------------------- 切片查询 -->
      <section class="panel">
        <div class="panel__head">
          <div class="panel__head-text">
            <h2 class="panel__title">切片查询</h2>
            <p class="panel__desc">调分块参数时用来核对切出来的内容与元数据</p>
          </div>
        </div>

        <div class="panel__body">
          <div class="row">
            <el-input
              v-model="chunkQuery.q"
              size="small"
              class="row__field"
              placeholder="按内容模糊搜索"
              clearable
            />
            <el-input-number
              v-model="chunkQuery.docId"
              size="small"
              placeholder="doc_id"
              :min="1"
              controls-position="right"
            />
            <el-input-number
              v-model="chunkQuery.page"
              size="small"
              placeholder="页码"
              :min="1"
              controls-position="right"
            />
            <el-button size="small" type="primary" :loading="chunkLoading" @click="searchChunks">
              查询
            </el-button>
          </div>

          <div v-if="chunkError" class="note note--error">
            <p class="note__title">查询失败：{{ chunkError.code }}</p>
            <p class="note__text">{{ humanizeError(chunkError) }}</p>
          </div>

          <template v-else>
            <el-table :data="chunks" size="small" max-height="420" class="chunks">
              <el-table-column prop="chunk_id" label="切片 ID" width="120" />
              <el-table-column prop="doc" label="文档" width="160" show-overflow-tooltip />
              <el-table-column prop="version" label="版本" width="80" />
              <el-table-column prop="section" label="章节" width="90" />
              <el-table-column prop="page" label="页码" width="70" />
              <el-table-column prop="token_len" label="Token" width="80" />
              <el-table-column prop="text" label="内容" min-width="280" show-overflow-tooltip />
              <template #empty>没有匹配的切片</template>
            </el-table>

            <el-pagination
              v-if="chunkTotal > chunkLimit"
              class="chunks__pager"
              layout="prev, pager, next, total"
              size="small"
              :current-page="Math.floor(chunkOffset / chunkLimit) + 1"
              :page-size="chunkLimit"
              :total="chunkTotal"
              @current-change="onChunkPage"
            />
          </template>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped>
.admin {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  background: var(--surface-1);
}

/* 限宽居中：管理页表单多，宽屏下铺满会让 label 与输入框离得非常远 */
.admin__inner {
  display: flex;
  flex-direction: column;
  gap: var(--sp-4);
  width: 100%;
  max-width: 1040px;
  padding: var(--sp-6) var(--sp-5) var(--sp-10);
  margin: 0 auto;
}

.admin__page-head {
  margin-bottom: var(--sp-1);
}

.admin__page-title {
  margin: 0;
  font-size: var(--fs-xl);
  font-weight: 650;
  letter-spacing: -0.4px;
  color: var(--ink-900);
}

.admin__page-desc {
  margin: var(--sp-1) 0 0;
  font-size: var(--fs-sm);
  color: var(--ink-500);
}

/* ------------------------------------------------------------------ 面板 */
/* 玻璃面板：半透明底 + 极淡的青色描边。管理页一整列都是面板，
   描边一旦明显就会变成一堆框；压到 12% 透明度后它只负责「收边」 */
.panel {
  background: rgba(22, 27, 34, 0.72);
  backdrop-filter: blur(10px);
  border: 1px solid rgba(0, 212, 255, 0.12);
  border-radius: var(--r-lg);
  box-shadow: var(--sh-sm);
}

.panel__head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-4);
  padding: var(--sp-4) var(--sp-5);
  border-bottom: 1px solid var(--line-1);
}

.panel__head-text {
  min-width: 0;
}

.panel__title {
  margin: 0;
  font-size: var(--fs-base);
  font-weight: 600;
  color: var(--ink-900);
}

.panel__desc {
  margin: 3px 0 0;
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--ink-500);
}

.panel__body {
  padding: var(--sp-5);
}

/* ------------------------------------------------------------------ 通用行 */
.row {
  display: flex;
  flex-wrap: wrap;
  gap: var(--sp-2);
  align-items: center;
}

.row__field {
  max-width: 280px;
}

.row__field--sm {
  width: 140px;
}

.hint {
  margin: 6px 0 0;
  font-size: var(--fs-xs);
  color: var(--ink-400);
}

/* -------------------------------------------------------------- 状态徽标 */
.health-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--sp-3);
  margin-bottom: var(--sp-4);
}

.health-meta {
  font-size: var(--fs-xs);
  color: var(--ink-400);
}

.health-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 9px 2px 7px;
  font-size: var(--fs-xs);
  font-weight: 600;
  text-transform: lowercase;
  border: 1px solid;
  border-radius: var(--r-pill);
}

.health-badge__dot {
  width: 5px;
  height: 5px;
  background: currentcolor;
  border-radius: 50%;
}

.health-badge--ok {
  color: var(--ok-600);
  background: var(--ok-50);
  border-color: rgba(45, 212, 191, 0.3);
}
.health-badge--warn {
  color: var(--warn-600);
  background: var(--warn-50);
  border-color: rgba(251, 191, 36, 0.35);
}
.health-badge--error {
  color: var(--danger-600);
  background: var(--danger-50);
  border-color: rgba(248, 113, 113, 0.32);
}

/* -------------------------------------------------------------- 组件清单 */
/* auto-fill + minmax：宽屏铺三到四列，窄屏自动落成一列，
   不写任何媒体查询 —— 卡片数是不定的（后端可能加组件） */
.components {
  display: grid;
  gap: var(--sp-3);
  padding: 0;
  margin: 0;
  list-style: none;
  grid-template-columns: repeat(auto-fill, minmax(228px, 1fr));
}

.components__card {
  padding: 11px var(--sp-4) 12px;
  background: rgba(22, 27, 34, 0.7);
  backdrop-filter: blur(10px);
  /* 顶边线用 2px 状态色，其余三边保持中性描边：
     故障卡片靠这一条线就能在一屏里跳出来，不必整张变红 */
  border: 1px solid var(--line-1);
  border-top: 2px solid var(--ink-300);
  border-radius: var(--r-md);
}

.components__card--ok {
  border-top-color: var(--ok-600);
}

.components__card--bad {
  background: rgba(248, 113, 113, 0.06);
  border-top-color: var(--danger-600);
}

.components__head {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
  margin-bottom: 6px;
}

.components__name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--ink-900);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.components__tone {
  flex: 0 0 auto;
  font-family: 'JetBrains Mono', Consolas, Menlo, monospace;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.6px;
  color: var(--ink-400);
}

.components__card--ok .components__tone {
  color: var(--ok-600);
}

.components__card--bad .components__tone {
  color: var(--danger-600);
}

/* 详情是运行时的原始字符串（地址、模型名、条数），保留原样但压到两行内 */
.components__detail {
  display: -webkit-box;
  overflow: hidden;
  line-height: 1.55;
  color: var(--ink-500);
  word-break: break-all;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

/* ------------------------------------------------------------------ 提示条 */
.note {
  padding: 10px var(--sp-4);
  margin-bottom: var(--sp-4);
  border: 1px solid;
  border-radius: var(--r-md);
}

.note--warn {
  background: var(--warn-50);
  border-color: rgba(251, 191, 36, 0.3);
}

.note--error {
  background: var(--danger-50);
  border-color: rgba(248, 113, 113, 0.32);
}

.note__title {
  margin: 0 0 3px;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--warn-600);
}

.note--error .note__title {
  color: var(--danger-600);
}

.note__text {
  margin: 0;
  font-size: var(--fs-sm);
  color: var(--ink-500);
}

/* ---------------------------------------------------------------- 表单区 */
/* 标题 / 版本 / 分类 / 上传人 两列排布，窄屏自动落成一列 */
.doc-form__grid {
  display: grid;
  gap: 0 var(--sp-5);
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
}

.doc-form :deep(.el-form-item) {
  margin-bottom: var(--sp-4);
}

/* ---------------------------------------------------------------- 任务进度 */
/* 轨道压到 surface-3、填充走青色渐变 + 外发光：
   EP 默认的轨道在深色底上几乎看不见，「进度到哪了」反而读不出来 */
.panel :deep(.el-progress-bar__outer) {
  background: var(--surface-3);
}

.panel :deep(.el-progress-bar__inner) {
  background-image: linear-gradient(90deg, #00b4d8, #00d4ff);
  box-shadow: 0 0 10px rgba(0, 212, 255, 0.35);
}

/* ---------------------------------------------------------------- 任务结果 */
.job-result {
  margin-top: var(--sp-3);
}

.job-result summary {
  font-size: var(--fs-xs);
  color: var(--ink-500);
  cursor: pointer;
  user-select: none;
}

.job-result summary:hover {
  color: var(--brand-600);
}

.job-result__pre {
  max-height: 260px;
  padding: var(--sp-3);
  margin: var(--sp-2) 0 0;
  overflow: auto;
  font-size: var(--fs-xs);
  line-height: 1.6;
  color: var(--ink-700);
  background: var(--surface-1);
  border: 1px solid var(--line-1);
  border-radius: var(--r-sm);
}

/* ---------------------------------------------------------------- 切片表 */
.chunks {
  margin-top: var(--sp-4);
}

.chunks__pager {
  justify-content: flex-end;
  margin-top: var(--sp-3);
}
</style>
