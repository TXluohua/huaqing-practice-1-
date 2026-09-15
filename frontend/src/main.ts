/**
 * 应用入口。
 *
 * 全局错误兜底放在这里而不是 App.vue：`app.config.errorHandler` 能兜住
 * 渲染之外（watcher、生命周期、事件回调）抛出的异常，这是 onErrorCaptured
 * 覆盖不到的部分。两者配合才谈得上「不白屏」。
 */

import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from '@/App.vue'
import router from '@/router'
import { humanizeError } from '@/api/http'

import 'element-plus/dist/index.css'
// 代码块主题与 styles/index.css 里 .markdown-body pre 的深色底配套
import 'highlight.js/styles/atom-one-dark.css'
import '@/styles/index.css'

const app = createApp(App)

app.use(createPinia())
app.use(router)
// 中文语言包：分页、表格空态等内置文案默认是英文
app.use(ElementPlus, { locale: zhCn })

app.config.errorHandler = (error, _instance, info) => {
  console.error('[app] 未捕获异常：', info, error)
}

// 未处理的 Promise 拒绝（例如某个忘了 catch 的接口调用）不应静默消失
window.addEventListener('unhandledrejection', (event) => {
  console.error('[app] 未处理的 Promise 拒绝：', humanizeError(event.reason))
})

app.mount('#app')
