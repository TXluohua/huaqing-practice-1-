<script setup lang="ts">
/**
 * 应用外壳：顶栏导航 + 全局健康自检 + 崩溃兜底。
 *
 * 为什么要在挂载时打一次 /api/health：
 * 后端没起时 Vite 代理会返回一个空 body 的 500，所有业务请求都会失败。
 * 如果不提前探测，用户看到的是「提问没反应」，而真正的原因在控制台里。
 * 这里把原因提到顶栏，并把启动命令一并给出。
 *
 * 只探测一次 + 手动重试，不做轮询：轮询会在后端长期不可用时刷满日志，
 * 而这个提示的作用只是「告诉你后端没起」。
 */
import { Loading } from '@element-plus/icons-vue'
import { computed, onErrorCaptured, onMounted, ref } from 'vue'

import { health, COMPONENT_LABELS, type HealthResponse } from '@/api/admin'
import { humanizeError, isApiError, type ApiError } from '@/api/http'

const backendDown = ref<ApiError | null>(null)
const healthData = ref<HealthResponse | null>(null)
const checking = ref(false)
/** 子组件树崩溃时的兜底标记 */
const crashed = ref(false)

const degraded = ref<string[]>([])

async function checkHealth(): Promise<void> {
  checking.value = true
  try {
    const data = await health()
    healthData.value = data
    backendDown.value = null
    degraded.value = Object.entries(data.components)
      .filter(([, item]) => !item.ok)
      .map(([name]) => COMPONENT_LABELS[name] ?? name)
  } catch (error) {
    healthData.value = null
    degraded.value = []
    if (isApiError(error)) backendDown.value = error
    else backendDown.value = null
  } finally {
    checking.value = false
  }
}

/** 服务状态的语义色彩：正常绿 / 降级黄 / 未知灰 / 不可达红 */
const healthTone = computed(() => {
  if (backendDown.value) return 'down'
  if (!healthData.value) return 'unknown'
  return healthData.value.status === 'ok' ? 'ok' : 'degraded'
})

const healthText = computed(() => {
  if (backendDown.value) return '后端未连接'
  if (!healthData.value) return '未检查'
  return healthData.value.status === 'ok' ? '服务正常' : '服务降级'
})

/** 模板里访问不到 window，刷新动作包一层 */
function reload(): void {
  window.location.reload()
}

/** 捕获子树崩溃：渲染 el-result 兜底，避免整页白屏 */
onErrorCaptured((error) => {
  console.error('[app] 组件树未捕获异常：', error)
  crashed.value = true
  return false
})

onMounted(() => void checkHealth())
</script>

<template>
  <div class="app scanlines">
    <header class="app__header">
      <div class="app__brand">
        <!-- 芯片状的品牌标记：比一个色块字母更能说明「半导体设备」这个领域 -->
        <span class="app__logo" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="17" height="17" fill="none">
            <rect
              x="6.5"
              y="6.5"
              width="11"
              height="11"
              rx="2.5"
              stroke="currentColor"
              stroke-width="1.7"
            />
            <rect x="10.2" y="10.2" width="3.6" height="3.6" rx="1" fill="currentColor" />
            <path
              d="M9.5 3.2v3.3M14.5 3.2v3.3M9.5 17.5v3.3M14.5 17.5v3.3M3.2 9.5h3.3M3.2 14.5h3.3M17.5 9.5h3.3M17.5 14.5h3.3"
              stroke="currentColor"
              stroke-width="1.7"
              stroke-linecap="round"
            />
          </svg>
        </span>
        <span class="app__brand-text">
          <span class="app__title">半导体设备维护知识库</span>
          <span class="app__subtitle">智能问答与溯源系统</span>
        </span>
      </div>

      <nav class="app__nav">
        <router-link to="/chat" class="app__nav-item">问答</router-link>
        <router-link to="/history" class="app__nav-item">历史会话</router-link>
        <router-link to="/admin" class="app__nav-item">管理</router-link>
      </nav>

      <div class="app__status">
        <button
          v-if="!healthData"
          class="app__health"
          type="button"
          :disabled="checking"
          @click="checkHealth"
        >
          <el-icon v-if="checking" class="is-loading"><Loading /></el-icon>
          <span class="app__health-dot" :class="`app__health-dot--${healthTone}`" />
          {{ healthText }}
        </button>

        <el-tooltip
          v-else
          placement="bottom"
          :content="`点击重新检查（${healthText}）`"
          :show-after="300"
        >
          <button class="app__health" type="button" :disabled="checking" @click="checkHealth">
            <el-icon v-if="checking" class="is-loading"><Loading /></el-icon>
            <span class="app__health-dot" :class="`app__health-dot--${healthTone}`" />
            {{ healthText }}
          </button>
        </el-tooltip>
      </div>
    </header>

    <!-- 后端未启动：给出可执行的启动命令，而不是一句「请求失败」 -->
    <el-alert
      v-if="backendDown"
      class="app__banner"
      type="error"
      :closable="false"
      show-icon
      :title="`后端未就绪（${backendDown.code}）`"
    >
      <div class="app__banner-body">
        <p>{{ humanizeError(backendDown) }}</p>
        <p class="app__banner-cmd text-mono">
          uvicorn backend.main:app --reload --port 8000
        </p>
        <el-button size="small" type="primary" plain :loading="checking" @click="checkHealth">
          重新检查
        </el-button>
      </div>
    </el-alert>

    <!-- 降级但不影响主链路（开发文档 R7）：MCP / 检查点等失败不应打断问答 -->
    <el-alert
      v-else-if="degraded.length"
      class="app__banner"
      type="warning"
      :closable="true"
      show-icon
      title="部分组件降级"
      :description="`以下组件不可用，主链路仍可用：${degraded.join('、')}。详见「管理」页。`"
    />

    <main class="app__main">
      <el-result
        v-if="crashed"
        icon="error"
        title="界面出现未预期的错误"
        sub-title="子组件渲染失败，请刷新页面重试；若反复出现请查看控制台与后端日志。"
      >
        <template #extra>
          <el-button type="primary" @click="reload">刷新页面</el-button>
        </template>
      </el-result>
      <router-view v-else />
    </main>
  </div>
</template>

<style scoped>
.app {
  /* .scanlines 的 ::before 是绝对定位，需要一个定位祖先 */
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

/* ------------------------------------------------------------------ 顶栏 */
.app__header {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: var(--sp-6);
  height: var(--app-header-h);
  padding: 0 var(--sp-6);
  background: rgba(13, 17, 23, 0.85);
  backdrop-filter: saturate(180%) blur(16px);
  border-bottom: 1px solid var(--line-1);
  box-shadow: 0 1px 0 0 rgba(0, 212, 255, 0.08);
}

.app__brand {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 10px;
}

/* 品牌标记：青蓝渐变 + 发光 */
.app__logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 30px;
  height: 30px;
  color: var(--on-brand);
  background: linear-gradient(140deg, #00d4ff, #0066ff);
  border-radius: 9px;
  box-shadow: 0 2px 8px rgba(0, 212, 255, 0.3), 0 0 12px rgba(0, 212, 255, 0.15);
}

.app__brand-text {
  display: flex;
  flex-direction: column;
  line-height: 1.25;
}

.app__title {
  font-size: var(--fs-base);
  font-weight: 600;
  letter-spacing: -0.1px;
  color: var(--ink-900);
}

.app__subtitle {
  font-size: 11px;
  color: var(--ink-400);
  letter-spacing: 0.2px;
}

/* -------------------------------------------------- 导航：分段控件式 */
.app__nav {
  display: flex;
  gap: 2px;
  padding: 3px;
  /* 宽度随内容收缩 + 左右 auto 外边距 = 居中。
     不要加 flex: 1：那会让 flex-basis 变成 0 并吃满剩余空间，
     fit-content 失效，分段控件在宽屏上被拉成一条长条。 */
  width: fit-content;
  margin: 0 auto;
  background: var(--surface-2);
  border-radius: var(--r-md);
}

.app__nav-item {
  padding: 5px 16px;
  font-size: var(--fs-sm);
  font-weight: 500;
  color: var(--ink-500);
  text-decoration: none;
  white-space: nowrap;
  border-radius: 7px;
  transition: background 0.16s var(--ease), color 0.16s var(--ease),
    box-shadow 0.16s var(--ease);
}

.app__nav-item:hover {
  color: var(--ink-900);
}

/* router-link-active 的类名由 vue-router 自动加上 */
.app__nav-item.router-link-active {
  color: var(--brand-400);
  background: var(--surface-0);
  box-shadow: 0 0 12px rgba(0, 212, 255, 0.15), var(--sh-xs);
}

/* ------------------------------------------------------------ 服务状态 */
.app__status {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
}

.app__health {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 11px;
  font-family: inherit;
  font-size: var(--fs-xs);
  color: var(--ink-500);
  cursor: pointer;
  /* 用 surface-2 而不是 surface-1：顶栏本身就是近 #0d1117 的深色，
     同色底会让这个按钮看起来只是一段文字，失去「可点」的暗示 */
  background: var(--surface-2);
  border: 1px solid var(--line-1);
  border-radius: var(--r-pill);
  transition: background 0.16s var(--ease), border-color 0.16s var(--ease),
    color 0.16s var(--ease), box-shadow 0.16s var(--ease);
}

.app__health:hover:not(:disabled) {
  color: var(--ink-900);
  background: var(--surface-3);
  border-color: var(--line-1);
  box-shadow: var(--glow-2);
}

.app__health:disabled {
  cursor: default;
  opacity: 0.75;
}

.app__health-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  box-shadow: 0 0 6px currentcolor, 0 0 0 3px currentcolor;
  opacity: 0.95;
}

.app__health-dot--ok {
  color: rgba(45, 212, 191, 0.2);
  background: var(--ok-600);
  animation: health-pulse 2s ease-in-out infinite;
}
.app__health-dot--degraded {
  color: rgba(251, 191, 36, 0.2);
  background: var(--warn-600);
}
.app__health-dot--down {
  color: rgba(248, 113, 113, 0.2);
  background: var(--danger-600);
}
.app__health-dot--unknown {
  color: rgba(139, 148, 158, 0.2);
  background: var(--ink-400);
}

@keyframes health-pulse {
  0%, 100% { opacity: 0.95; }
  50% { opacity: 0.6; }
}

/* ---------------------------------------------------------------- 横幅 */
/* 横幅通栏但不贴边，与页面元素的左右边距对齐 */
.app__banner {
  margin: var(--sp-3) var(--sp-6) 0;
  border-radius: var(--r-md);
}

.app__banner-body p {
  margin: 4px 0;
  font-size: var(--fs-sm);
}

.app__banner-cmd {
  display: inline-block;
  padding: 6px 10px;
  color: var(--ink-900);
  background: rgba(0, 0, 0, 0.35);
  border: 1px solid var(--line-1);
  border-radius: var(--r-sm);
}

.app__main {
  display: flex;
  flex: 1;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
}
</style>
