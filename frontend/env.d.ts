/// <reference types="vite/client" />

/**
 * 让 TS 认识 .vue 单文件组件。
 *
 * 没有这段声明时，`import MessageItem from './MessageItem.vue'` 会因为
 * 找不到模块声明而报错（Vite 运行时能解析，但 vue-tsc 与 IDE 不能）。
 */
declare module '*.vue' {
  import type { DefineComponent } from 'vue'

  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}
