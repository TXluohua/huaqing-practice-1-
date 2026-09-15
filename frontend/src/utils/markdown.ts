/**
 * Markdown 渲染器（问答页与历史页共用）。
 *
 * 抽出来的原因：同一个答案在两处必须长得一样。原先只有 MessageItem 走 markdown，
 * 历史页用 pre-wrap 直接把文本铺出来，于是同一段回答在问答页有排版、有可点的 [1]
 * 角标，在历史页却是一堆裸文本和字面的「[1]」——同一条数据两种呈现是明显的不一致。
 *
 * 两条安全约束（改动时不要放宽）：
 *
 *   1. **html: false**。模型输出与历史落库内容都属不可信输入，
 *      放开内联 HTML 等于把 XSS 口子开在渲染管线最深处。
 *
 *   2. **引用角标在 token 层切分，不对渲染后的 HTML 跑正则**。
 *      `html.replace(/\[(\d+)\]/g, ...)` 会连带改掉代码块里的 `data[1]`、
 *      链接文本里的 `[1]`，把内容改错。只处理 type==='text' 的 token 才是安全的。
 */
import hljs from 'highlight.js/lib/common'
import MarkdownIt from 'markdown-it'

/** 兜底转义。刻意不用 md.utils.escapeHtml：那会让 md 的初始化引用自身 */
function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

export const md: MarkdownIt = new MarkdownIt({
  html: false, // 不可信内容，禁止内联 HTML
  linkify: true,
  breaks: true,
  highlight(code: string, lang: string): string {
    try {
      if (lang && hljs.getLanguage(lang)) {
        return hljs.highlight(code, { language: lang, ignoreIllegals: true }).value
      }
      return hljs.highlightAuto(code).value
    } catch {
      // 高亮失败不能让整段渲染失败，退回纯文本转义
      return escapeHtml(code)
    }
  },
})

/**
 * 把正文里的 [n] 变成可点击的角标。
 *
 * 只处理 type==='text' 的 token —— code_inline / fence / link 等类型的
 * content 不在 children 的 text token 里，天然不受影响。
 */
md.core.ruler.push('citation_ref', (state) => {
  for (const token of state.tokens) {
    if (token.type !== 'inline' || !token.children) continue

    const rebuilt: typeof token.children = []
    for (const child of token.children) {
      if (child.type !== 'text') {
        rebuilt.push(child)
        continue
      }

      const text = child.content
      let cursor = 0
      let matched = false

      // 每次都新建正则，避免 lastIndex 在多次渲染间串味
      for (const match of text.matchAll(/\[(\d+)\]/g)) {
        matched = true
        const start = match.index ?? 0
        if (start > cursor) {
          const plain = new state.Token('text', '', 0)
          plain.content = text.slice(cursor, start)
          rebuilt.push(plain)
        }
        const sup = new state.Token('html_inline', '', 0)
        // 只插入纯数字，不存在注入风险
        sup.content = `<sup class="cite-ref" data-cite-id="${match[1]}">[${match[1]}]</sup>`
        rebuilt.push(sup)
        cursor = start + match[0].length
      }

      if (!matched) {
        rebuilt.push(child)
        continue
      }
      if (cursor < text.length) {
        const tail = new state.Token('text', '', 0)
        tail.content = text.slice(cursor)
        rebuilt.push(tail)
      }
    }
    token.children = rebuilt
  }
})

/** 渲染答案正文（含引用角标标记） */
export function renderMarkdown(text: string): string {
  return md.render(text)
}

/**
 * 从点击事件里取出被点的引用编号。
 *
 * **按 id 查而不是按数组下标**：`[3]` 指的是编号为 3 的引用，
 * 与它在 citations 数组里的位置无关（后端分配的编号顺序与展示顺序未必一致）。
 */
export function citationIdFromEvent(event: MouseEvent): number | null {
  const target = (event.target as HTMLElement | null)?.closest('[data-cite-id]')
  if (!target) return null
  const id = Number(target.getAttribute('data-cite-id'))
  return Number.isFinite(id) ? id : null
}
