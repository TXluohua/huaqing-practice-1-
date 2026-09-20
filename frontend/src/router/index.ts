/**
 * 路由。
 *
 * 分**两组、互相独立**：
 *   - 问答组：`/chat`、`/history`、`/admin`（知识库问答与溯源，走 SSE）；
 *   - 业务组：`/plans`、`/parts`、`/procurement`、`/training`
 *     （维护计划生成、备件商城、采购结算、考核认证）。
 *
 * 为什么业务页要做成独立路由，而不是塞进问答页：
 *   这四块功能与问答链路**没有任何依赖** —— 不用会话 id、不用 SSE、不用 qa_id，
 *   各自只调用自己的 `/api/plans`、`/api/parts`、`/api/training` 接口。
 *   放在独立路由上，它们才能被单独进入、单独演示、后续单独拆包，
 *   也不会因为问答链路（模型 / 向量库 / MCP）降级而被一起拖下水。
 *
 * 不引入 `/history/:id` 与业务页的子路由：业务页内部用 Tab / 抽屉表达层次，
 * 选中状态是页面内状态而非路由状态。
 */

import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/chat' },
  {
    path: '/chat',
    name: 'chat',
    component: () => import('@/views/ChatView.vue'),
    meta: { title: '问答' },
  },
  {
    path: '/history',
    name: 'history',
    component: () => import('@/views/HistoryView.vue'),
    meta: { title: '历史会话' },
  },
  {
    path: '/admin',
    name: 'admin',
    component: () => import('@/views/AdminView.vue'),
    meta: { title: '管理' },
  },
  // ---------------- 业务组：与问答完全独立的四个功能页 ----------------
  {
    path: '/plans',
    name: 'plans',
    component: () => import('@/views/PlanView.vue'),
    meta: { title: '维护计划', group: 'biz' },
  },
  {
    path: '/parts',
    name: 'parts',
    component: () => import('@/views/PartsView.vue'),
    meta: { title: '备件商城', group: 'biz' },
  },
  {
    path: '/procurement',
    name: 'procurement',
    component: () => import('@/views/ProcurementView.vue'),
    meta: { title: '采购结算', group: 'biz' },
  },
  {
    path: '/training',
    name: 'training',
    component: () => import('@/views/TrainingView.vue'),
    meta: { title: '考核认证', group: 'biz' },
  },
  // 兜底：未知路径回问答页，而不是留一个空白页
  { path: '/:pathMatch(.*)*', redirect: '/chat' },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  const title = (to.meta.title as string | undefined) ?? ''
  document.title = title ? `${title} · 半导体设备维护知识库` : '半导体设备维护知识库智能问答系统'
})

export default router
