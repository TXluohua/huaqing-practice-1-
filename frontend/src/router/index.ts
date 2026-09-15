/**
 * 路由（开发文档 §5.5：/chat、/history、/admin）。
 *
 * 只保留三条路由，不引入 /history/:id —— 历史页用「左列表 + 右详情」的主从布局，
 * 会话选中状态是页面内状态而非路由状态。这样「继续对话」跳回 /chat 时
 * 不需要再处理一个返回路径。
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
