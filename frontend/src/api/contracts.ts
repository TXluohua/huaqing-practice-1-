/**
 * 跨模块共享的**数据契约类型**（与具体页面无关）。
 *
 * 为什么单独一个文件：引用项（`Citation`）既是问答回答的组成部分，也是维护计划的
 * `evidence`、考卷每题的依据、备件替代件的依据来源。如果把它定义在 `@/api/chat` 里，
 * 那么「维护计划 / 备件商城 / 考核认证」这三块**独立业务**就会被迫 import 问答模块
 * （哪怕只是类型），页面独立性与后续拆包都会受影响。
 *
 * 因此：共享类型放这里，`@/api/chat` 再 re-export 一次以保持既有引用可用；
 * 三块业务的 API 模块一律只从本文件取类型。
 *
 * 字段与 `backend/agents/state.py` 的 `Citation` 完全一致。
 */

/** 引用来源类型（接口文档 §3.1） */
export type SourceType = 'kb_doc' | 'ticket' | 'image'

/** 引用项。字段与 backend/agents/state.py 的 Citation 完全一致。 */
export interface Citation {
  /** 引用编号 [n]，由后端代码分配，禁止模型编造 */
  id: number
  source_type: SourceType
  doc: string | null
  /** 文档版本 —— P5「版本混乱」痛点的关键字段，展示时不要省略 */
  version: string | null
  section: string | null
  page: number | null
  /** 切片 ID，用于「查看原文」 */
  chunk_id: string | null
  /** 图片类依据的原图地址（/static/uploads/...，dev 由 Vite 代理） */
  image_url: string | null
  snippet: string | null
}
