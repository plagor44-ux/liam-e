// Synthetic layout regression. Run with Playwright available on NODE_PATH.
const assert = require('node:assert/strict')
const { execFileSync } = require('node:child_process')
const { resolve } = require('node:path')
const { chromium } = require('playwright')

async function main() {
  const web = resolve(__dirname, '../apps/web')
  const markup = execFileSync('bun', ['-e', `
    import { createElement as h } from 'react'
    import { renderToStaticMarkup } from 'react-dom/server'
    import { Notice } from './src/ds/Notice'
    const long = 'Mail actions are paused. Reconnect the account in Mistral Studio and allow another attempt from Accounts.'
    const notice = (id, text, withAction, toast) => h(
      'section', { id, className: toast ? 'le-inbox-toast le-inbox-toast--inline' : undefined },
      h(Notice, { tone: toast ? 'danger' : 'info', action: withAction ? { label: 'Open Accounts', onClick() {} } : undefined }, text),
    )
    const content = h('main', { style: { maxWidth: '20rem' } },
      notice('short', 'Done.'),
      notice('accounts', long, true),
      notice('reader', h('span', null, long)),
      notice('toast', h('span', { className: 'le-inbox-toast-text--wrap' }, long), false, true),
    )
    console.log(renderToStaticMarkup(content))
  `], { cwd: web, encoding: 'utf8' })
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', args: ['--no-sandbox'] })
  try {
    for (const width of [390, 1280]) {
      const page = await browser.newPage({ viewport: { width, height: 844 }, reducedMotion: 'reduce' })
      await page.route('**/*', route => route.abort())
      await page.setContent('<!doctype html><html><body style="font: 16px/1.5 sans-serif">' + markup + '</body></html>')
      for (const css of ['tokens.css', 'ds.css', 'ds-app.css']) await page.addStyleTag({ path: resolve(web, 'src/styles', css) })
      for (const id of ['short', 'accounts', 'reader', 'toast']) {
        const result = await page.locator(`#${id} .le-notice`).evaluate(notice => {
          const icon = notice.querySelector('svg').getBoundingClientRect()
          const walker = document.createTreeWalker(notice, NodeFilter.SHOW_TEXT)
          let text
          do { text = walker.nextNode() } while (text && !text.textContent.trim())
          const range = document.createRange()
          range.selectNodeContents(text)
          const rects = [...range.getClientRects()].map(({ x, y, height }) => ({ x, y, height }))
          return { icon: { right: icon.right, y: icon.y, height: icon.height }, rects }
        })
        const { icon, rects } = result
        const first = rects[0]
        if (id === 'short') assert.equal(rects.length, 1)
        else assert.ok(rects.length > 1, `${id} should wrap at viewport ${width}`)
        assert.ok(icon.right < first.x, `${id} icon should be beside the first line at viewport ${width}`)
        assert.ok(Math.abs(icon.y + icon.height / 2 - first.y - first.height / 2) <= 4,
          `${id} icon should align vertically with the first line at viewport ${width}`)
        assert.ok(rects.every(line => Math.abs(line.x - first.x) <= 1), `${id} continuation lines should align at viewport ${width}`)
      }
      await page.close()
    }
    console.log('Notice layout: single line and three wrapped surfaces passed at desktop and mobile widths')
  } finally {
    await browser.close()
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
