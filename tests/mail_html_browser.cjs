// Synthetic Chromium security contract. Run with Playwright available on NODE_PATH.
const { chromium } = require('playwright')
const { execFileSync } = require('node:child_process')
const { mkdtempSync, writeFileSync, rmSync } = require('node:fs')
const { tmpdir } = require('node:os')
const { join, resolve } = require('node:path')
const http = require('node:http')

async function main() {
  const root = resolve(__dirname, '..')
  const scratch = mkdtempSync(join(tmpdir(), 'liam-mail-html-'))
  const entry = join(scratch, 'entry.ts')
  const bundle = join(scratch, 'bundle.js')
  writeFileSync(entry, `import { prepareMailDocument } from ${JSON.stringify(join(root, 'apps/web/src/mail/mail-html.ts'))}; window.prepareMailDocument = prepareMailDocument`)
  execFileSync('bun', ['build', entry, '--target', 'browser', '--outfile', bundle], { stdio: 'ignore' })
  let imageRequests = 0, cssRequests = 0
  const server = http.createServer((req, res) => {
    if (req.url === '/pixel') { imageRequests++; res.end('synthetic'); return }
    if (req.url === '/css') { cssRequests++; res.end(''); return }
    res.setHeader('Content-Type', 'text/html')
    res.end('<!doctype html><html><body><main></main></body></html>')
  })
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
  let browser
  try {
    browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', args: ['--no-sandbox'] })
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } })
    const origin = `http://127.0.0.1:${server.address().port}`
    await page.goto(origin)
    await page.addScriptTag({ path: bundle })
    const html = `<p class="sample">Sample</p><img src="${origin}/pixel"><img src="cid:part"><a href="${origin}/link">Open</a><style><!-- @import url(${origin}/css); @media (prefers-color-scheme: dark) { .sample { color:#fff; background:url(${origin}/css) } } --></style><script>parent.compromised=true</script>`
    for (const dark of [false, true]) for (const images of [false, true]) {
      imageRequests = 0; cssRequests = 0
      await page.evaluate(({ html, images, dark }) => {
        const frame = document.createElement('iframe')
        frame.sandbox = 'allow-popups allow-popups-to-escape-sandbox'
        frame.srcdoc = window.prepareMailDocument(html, images, 'Unavailable', dark).document
        document.querySelector('main').replaceChildren(frame)
      }, { html, images, dark })
      await page.waitForTimeout(400)
      if (cssRequests !== 0) throw Error('CSS attempted a network request')
      if (imageRequests !== Number(images)) throw Error(`unexpected image requests for consent=${images}`)
      if (await page.evaluate(() => window.compromised === true)) throw Error('script escaped sandbox')
      if (!images) {
        const popupPromise = page.waitForEvent('popup')
        await page.frameLocator('iframe').getByRole('link', { name: 'Open' }).click()
        const popup = await popupPromise
        await popup.waitForURL('**/link')
        if (await popup.evaluate(() => window.opener !== null)) throw Error('popup has an opener')
        await popup.close()
      }
    }
    await page.evaluate(() => {
      const frame = document.createElement('iframe')
      frame.sandbox = 'allow-popups allow-popups-to-escape-sandbox'
      frame.srcdoc = window.prepareMailDocument('<style>@media(prefers-color-scheme:dark){.panel{background:#fff}.panel p{color:#000}}</style><div class="panel"><p>Visible</p></div>', false, 'Unavailable', true).document
      document.querySelector('main').replaceChildren(frame)
    })
    const panel = page.frameLocator('iframe').locator('.panel')
    const paragraph = panel.locator('p')
    await paragraph.waitFor()
    const colors = await paragraph.evaluate(node => [getComputedStyle(node.parentElement).backgroundColor, getComputedStyle(node).color])
    if (colors[0] !== 'rgb(255, 255, 255)' || colors[1] !== 'rgb(0, 0, 0)') throw Error(`unexpected separated-rule colours: ${colors}`)
    await page.evaluate(() => {
      const frame = document.createElement('iframe')
      frame.sandbox = 'allow-popups allow-popups-to-escape-sandbox'
      frame.srcdoc = window.prepareMailDocument('<style>@media(prefers-color-scheme:dark){html > body{background:#fff;color:#000}}</style><p>Visible</p>', false, 'Unavailable', true).document
      document.querySelector('main').replaceChildren(frame)
    })
    const frame = page.frameLocator('iframe')
    await frame.locator('p').waitFor()
    const canvas = await frame.locator('html').evaluate(node => getComputedStyle(node).backgroundColor)
    if (canvas !== 'rgb(255, 255, 255)') throw Error(`sender background does not cover canvas: ${canvas}`)
    console.log('Synthetic Chromium: image consent, script isolation and safe link opening passed')
  } finally {
    await browser?.close()
    server.close()
    rmSync(scratch, { recursive: true, force: true })
  }
}
main().catch(error => { console.error(error); process.exitCode = 1 })
