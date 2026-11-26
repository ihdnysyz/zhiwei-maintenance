<script setup lang="ts">
import { computed } from 'vue'
import { Marked, type TokenizerAndRendererExtension, type Tokens } from 'marked'
import DOMPurify from 'dompurify'
import hljs from 'highlight.js/lib/common'
import katex from 'katex'
import 'highlight.js/styles/github-dark.min.css'
import 'katex/dist/katex.min.css'

const props = withDefaults(defineProps<{
  content: string
  streaming?: boolean
  citeCount?: number
}>(), {
  streaming: false,
  citeCount: 0,
})

const emit = defineEmits<{ cite: [index: number] }>()

function escapeHtml(text: string) {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function renderKatex(expr: string, displayMode: boolean) {
  const cleaned = expr.trim()
  if (!cleaned) return ''
  try {
    const html = katex.renderToString(cleaned, {
      displayMode,
      output: 'html',
      throwOnError: false,
      strict: 'ignore',
      trust: false,
    })
    return displayMode
      ? `<div class="md-math-block">${html}</div>`
      : `<span class="md-math-inline">${html}</span>`
  } catch {
    return `<code>${escapeHtml(cleaned)}</code>`
  }
}

const blockMathExtension: TokenizerAndRendererExtension = {
  name: 'blockMath',
  level: 'block',
  start(src: string) {
    const idx1 = src.indexOf('$$')
    const idx2 = src.indexOf('\\[')
    if (idx1 === -1) return idx2
    if (idx2 === -1) return idx1
    return Math.min(idx1, idx2)
  },
  tokenizer(src: string) {
    const dollarMatch = /^\$\$([\s\S]+?)\$\$(?:\n+|$)/.exec(src)
    if (dollarMatch) {
      return {
        type: 'blockMath',
        raw: dollarMatch[0],
        text: dollarMatch[1].trim(),
      }
    }
    const bracketMatch = /^\\\[([\s\S]+?)\\\](?:\n+|$)/.exec(src)
    if (bracketMatch) {
      return {
        type: 'blockMath',
        raw: bracketMatch[0],
        text: bracketMatch[1].trim(),
      }
    }
    return undefined
  },
  renderer(token) {
    return renderKatex(String(token.text || ''), true)
  },
}

const inlineMathExtension: TokenizerAndRendererExtension = {
  name: 'inlineMath',
  level: 'inline',
  start(src: string) {
    const idx1 = src.indexOf('$')
    const idx2 = src.indexOf('\\(')
    if (idx1 === -1) return idx2
    if (idx2 === -1) return idx1
    return Math.min(idx1, idx2)
  },
  tokenizer(src: string) {
    // \( ... \) inline math
    const parenMatch = /^\\\(([^\n]+?)\\\)/.exec(src)
    if (parenMatch) {
      return {
        type: 'inlineMath',
        raw: parenMatch[0],
        text: parenMatch[1].trim(),
      }
    }
    // $...$ inline math (avoid matching currency like $100 or empty $$)
    const dollarMatch = /^\$(?!\$|\s)((?:[^$\\\n]|\\.)+?)(?<!\s)\$(?!\d)/.exec(src)
    if (dollarMatch) {
      const inner = dollarMatch[1]
      // Skip plain numbers with currency-like punctuation (e.g. $100 - $200)
      if (/^\d+(?:[.,]\d+)?$/.test(inner)) return undefined
      return {
        type: 'inlineMath',
        raw: dollarMatch[0],
        text: inner,
      }
    }
    return undefined
  },
  renderer(token) {
    return renderKatex(String(token.text || ''), false)
  },
}

const ALERT_META: Record<string, { cls: string; label: string }> = {
  NOTE: { cls: 'note', label: '说明' },
  TIP: { cls: 'tip', label: '建议' },
  IMPORTANT: { cls: 'important', label: '重要' },
  WARNING: { cls: 'warning', label: '警告' },
  CAUTION: { cls: 'caution', label: '安全注意' },
}

const md = new Marked({
  gfm: true,
  breaks: true,
})

md.use({
  extensions: [blockMathExtension, inlineMathExtension],
  renderer: {
    code(token: Tokens.Code) {
      const lang = (token.lang || '').trim().split(/\s+/)[0].toLowerCase()
      let highlighted = escapeHtml(token.text)
      let langClass = ''
      if (lang && hljs.getLanguage(lang)) {
        try {
          highlighted = hljs.highlight(token.text, { language: lang, ignoreIllegals: true }).value
          langClass = ` class="hljs language-${escapeHtml(lang)}"`
        } catch {
          highlighted = escapeHtml(token.text)
        }
      }
      const displayLang = lang || 'text'
      return (
        `<div class="md-code-block">`
        + `<div class="md-code-head">`
        + `<span>${escapeHtml(displayLang)}</span>`
        + `<button type="button" class="md-code-copy" data-copy-code="1">复制</button>`
        + `</div>`
        + `<pre><code${langClass}>${highlighted}</code></pre>`
        + `</div>`
      )
    },
    table(token: Tokens.Table) {
      const headerCells = token.header.map((cell, i) => {
        const align = token.align[i] ? ` style="text-align:${token.align[i]}"` : ''
        return `<th${align}>${this.parser.parseInline(cell.tokens)}</th>`
      }).join('')
      const bodyRows = token.rows.map(row => {
        const cells = row.map((cell, i) => {
          const align = token.align[i] ? ` style="text-align:${token.align[i]}"` : ''
          return `<td${align}>${this.parser.parseInline(cell.tokens)}</td>`
        }).join('')
        return `<tr>${cells}</tr>`
      }).join('')
      return `<div class="md-table-wrap"><table><thead><tr>${headerCells}</tr></thead><tbody>${bodyRows}</tbody></table></div>`
    },
    blockquote(token: Tokens.Blockquote) {
      const body = this.parser.parse(token.tokens)
      const alertMatch = /^\s*<p>\s*\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*(?:<br\s*\/?>|\n)?([\s\S]*?)<\/p>([\s\S]*)$/i.exec(body)
      if (alertMatch) {
        const key = alertMatch[1].toUpperCase()
        const meta = ALERT_META[key] || ALERT_META.NOTE
        const firstPara = alertMatch[2].trim() ? `<p>${alertMatch[2].trim()}</p>` : ''
        const rest = alertMatch[3] || ''
        return (
          `<div class="md-callout is-${meta.cls}">`
          + `<div class="md-callout-title">${meta.label}</div>`
          + `<div class="md-callout-body">${firstPara}${rest}</div>`
          + `</div>`
        )
      }
      return `<blockquote>${body}</blockquote>`
    },
    checkbox() {
      return ''
    },
    listitem(token: Tokens.ListItem) {
      const isTask = Boolean(token.task)
      const checkHtml = isTask
        ? (token.checked
          ? `<span class="md-task-check is-checked" aria-label="已完成">✓</span>`
          : `<span class="md-task-check" aria-label="未完成"></span>`)
        : ''
      const bodyTokens = isTask
        ? token.tokens.filter((t, idx) => !(idx === 0 && t.type === 'checkbox'))
        : token.tokens
      const content = this.parser.parse(bodyTokens)
      if (isTask) {
        const cleaned = content.replace(/^<p>/, '').replace(/<\/p>\s*$/, '').trim()
        return `<li class="md-task-item">${checkHtml}<span class="md-task-text">${cleaned}</span></li>\n`
      }
      return `<li>${content}</li>\n`
    },
    link(token: Tokens.Link) {
      const href = (token.href || '').trim()
      const label = this.parser.parseInline(token.tokens)
      if (!/^(?:https?:\/\/|mailto:)/i.test(href)) return label
      const title = token.title ? ` title="${escapeHtml(token.title)}"` : ''
      const isMail = /^mailto:/i.test(href)
      const attrs = isMail ? '' : ' target="_blank" rel="noopener noreferrer"'
      return `<a href="${escapeHtml(href)}"${title}${attrs}>${label}</a>`
    },
    image(token: Tokens.Image) {
      const href = (token.href || '').trim()
      const alt = escapeHtml(token.text || '')
      if (!/^(?:https?:\/\/|\/api\/)/i.test(href)) return alt
      const title = token.title ? ` title="${escapeHtml(token.title)}"` : ''
      return `<img class="md-img" src="${escapeHtml(href)}" alt="${alt}"${title} loading="lazy" />`
    },
  },
})

function normalizeStreamingMarkdown(raw: string, streaming: boolean) {
  if (!streaming) return raw
  // Close an unclosed fenced code block cleanly during streaming so rendering stays stable
  const fences = raw.match(/(^|\n)```/g)
  if (fences && fences.length % 2 === 1) {
    return `${raw}\n\`\`\``
  }
  return raw
}

function decorateCitations(html: string, maxCite: number) {
  if (!maxCite) return html
  // Split around code/pre/katex/tag segments so citations are only added in plain prose
  const parts = html.split(/(<pre[\s\S]*?<\/pre>|<code[\s\S]*?<\/code>|<span class="katex[\s\S]*?<\/span>|<[^>]+>)/gi)
  return parts.map((segment, idx) => {
    if (idx % 2 === 1) return segment
    return segment.replace(/\[(\d+)\]/g, (full, raw) => {
      const num = Number(raw)
      if (num >= 1 && num <= maxCite) {
        return `<button type="button" class="md-cite" data-cite="${num}" aria-label="查看引用 ${num}">[${num}]</button>`
      }
      return full
    })
  }).join('')
}

const rendered = computed(() => {
  const source = normalizeStreamingMarkdown(props.content || '', props.streaming)
  if (!source.trim()) return ''
  const rawHtml = md.parse(source, { async: false }) as string
  const withCites = decorateCitations(rawHtml, props.citeCount)
  return DOMPurify.sanitize(withCites, {
    USE_PROFILES: { html: true, svg: true },
    FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'form', 'style'],
    ADD_TAGS: ['button', 'details', 'summary'],
    ADD_ATTR: ['data-cite', 'data-copy-code', 'target', 'rel', 'type', 'aria-label', 'aria-hidden', 'loading', 'open'],
  })
})

async function copyCode(button: HTMLElement) {
  const block = button.closest('.md-code-block')
  const codeText = block?.querySelector('pre code')?.textContent || ''
  if (!codeText) return
  try {
    await navigator.clipboard.writeText(codeText)
  } catch {
    const area = document.createElement('textarea')
    area.value = codeText
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    document.execCommand('copy')
    document.body.removeChild(area)
  }
  button.textContent = '已复制'
  button.classList.add('is-copied')
  window.setTimeout(() => {
    button.textContent = '复制'
    button.classList.remove('is-copied')
  }, 1600)
}

function onClick(event: MouseEvent) {
  const target = event.target instanceof Element ? event.target : null
  if (!target) return
  const copyBtn = target.closest<HTMLElement>('[data-copy-code]')
  if (copyBtn) {
    void copyCode(copyBtn)
    return
  }
  const citeBtn = target.closest<HTMLElement>('[data-cite]')
  if (!citeBtn) return
  const num = Number(citeBtn.dataset.cite)
  if (Number.isInteger(num) && num >= 1) emit('cite', num)
}
</script>

<template>
  <div class="md-body" @click="onClick">
    <div v-if="rendered" v-html="rendered"></div>
    <span v-if="streaming" class="stream-cursor" aria-hidden="true"></span>
  </div>
</template>
