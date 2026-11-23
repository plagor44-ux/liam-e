import { expect, test } from 'bun:test'
import { buildMailDocument, prepareMailDocument } from './mail-html'

const hostile = `<script>alert(1)</script><p onclick="alert(2)">Hi</p><a href="javascript:alert(1)">bad</a><a href="https://example.test/page">link</a><meta http-equiv="refresh" content="0;url=https://evil.test"><form action="https://evil.test"><input></form><iframe src="https://evil.test"></iframe><style>@import url(https://evil.test/a)</style><p style="background:url(https://evil.test/b)">Hi</p><img src="https://images.example.test/x"><img src="cid:embedded">`

test('mail document blocks active markup and remote fetches until explicitly allowed', () => {
  const blocked = buildMailDocument(hostile, false, 'Image unavailable')
  expect(blocked).not.toContain('onclick')
  expect(blocked).not.toContain('javascript:')
  expect(blocked).not.toContain('evil.test')
  expect(blocked).not.toContain('images.example.test')
  expect(blocked).not.toContain('cid:')
  expect(blocked).toContain("img-src 'none'")
  expect(blocked).toContain("form-action 'none'")
  expect(blocked).toContain("frame-src 'none'")
  expect(blocked).toContain("base-uri 'none'")
  expect(blocked).toContain('target="_blank"')
  expect(blocked).toContain('rel="noopener noreferrer"')
  const allowed = buildMailDocument(hostile, true, 'Image unavailable')
  expect(allowed).toContain('https://images.example.test/x')
  expect(allowed).not.toContain('evil.test')
  expect(allowed).not.toContain('cid:')
})

test('only remote image sources offer image loading', () => {
  expect(prepareMailDocument('<p>Text</p><img src="cid:one">', false, 'Image unavailable').hasRemoteImages).toBe(false)
  expect(prepareMailDocument('<img src="https://example.test/pixel">', false, 'Image unavailable').hasRemoteImages).toBe(true)
})

test('dark appearance adapts light colors and preserves dark surfaces', () => {
  const raw = '<div style="background-color:#ffffff;color:#000000">Light</div><div style="background-color:#111111;color:#ffffff">Dark</div>'
  const dark = prepareMailDocument(raw, false, 'Image unavailable', true).document
  expect(dark).toContain('background-color: rgb(34, 45, 43)')
  expect(dark).toContain('background-color:#111111')
  expect(dark).toContain('color: rgb(232, 239, 236)')
  expect(prepareMailDocument(raw, false, 'Image unavailable').document).toContain('background-color:#ffffff')
})

test('dark media rules keep safe colours, strip remote CSS and do not affect light mode', () => {
  const raw = '<style>@media (prefers-color-scheme: dark) {.panel { background-color:#123456; color:#ffffff; background:url(https://evil.test/a) }}</style><div class="panel">Test</div>'
  const dark = prepareMailDocument(raw, false, 'Image unavailable', true).document
  expect(dark).toContain('background-color: rgb(18, 52, 86); color: rgb(255, 255, 255)')
  expect(dark).not.toContain('evil.test')
  expect(prepareMailDocument(raw, false, 'Image unavailable').document).not.toContain('.panel{')
})

test('dark styles repair low contrast and reject unsupported CSS', () => {
  const raw = '<style>@media (prefers-color-scheme:dark){.panel{background:#777777;color:#777777;background-image:url(https://evil.test/x)}} @import url(https://evil.test/y)</style><div class="panel">Test</div>'
  const result = prepareMailDocument(raw, false, 'Image unavailable', true).document
  expect(result).toContain('background-color: rgb(119, 119, 119); color: rgb(0, 0, 0)')
  expect(result).not.toContain('evil.test')
})

test('separate sender rules use the effective inherited background for contrast', () => {
  const raw = '<style>@media(prefers-color-scheme:dark){.panel{background:#fff}.panel p{color:#000}}</style><div class="panel"><p>Visible</p></div>'
  const result = prepareMailDocument(raw, false, 'Unavailable', true).document
  expect(result).toContain('background-color: rgb(255, 255, 255)')
  expect(result).toContain('color: rgb(0, 0, 0)')
})

test('more specific sender colour wins independently of declaration order', () => {
  const raw = '<style>@media(prefers-color-scheme:dark){.panel p{color:#000}.other{color:#fff}.panel{background:#fff}}</style><div class="panel"><p class="other">Visible</p></div>'
  const result = prepareMailDocument(raw, false, 'Unavailable', true).document
  expect(result).toContain('color: rgb(0, 0, 0)')
})

test('sender body rules apply to the message root and descendants', () => {
  const raw = '<style>@media(prefers-color-scheme:dark){body{background:#fff}body p{color:#000}}</style><p>Visible</p>'
  const result = prepareMailDocument(raw, false, 'Unavailable', true).document
  expect(result).toContain('background-color: rgb(255, 255, 255)')
  expect(result).toContain('color: rgb(0, 0, 0)')
})

test.each([
  '/* synthetic comment */ ',
  '@charset "UTF-8"; ',
  '@import url(https://evil.test/css); ',
  '<!-- ',
])('dark media rules survive a safe style prelude: %s', prelude => {
  const suffix = prelude === '<!-- ' ? ' -->' : ''
  const raw = `<style>${prelude}@media(prefers-color-scheme:dark){.panel{background:#123456;color:#fff}}${suffix}</style><div class="panel">Synthetic</div>`
  const result = prepareMailDocument(raw, false, 'Unavailable', true).document
  expect(result).toContain('background-color: rgb(18, 52, 86)')
  expect(result).not.toContain('evil.test')
})

test('important sender light background is resolved before inline descendant text', () => {
  const raw = '<style>@media(prefers-color-scheme:dark){.panel{background:#fff!important}}</style><div class="panel"><p style="color:#000">Visible</p></div>'
  const result = prepareMailDocument(raw, false, 'Unavailable', true).document
  expect(result).toContain('background-color: rgb(255, 255, 255)')
  expect(result).toContain('color:#000')
})

test('unterminated and nested CSS cannot cause backtracking or introduce rules', () => {
  const raw = `<style>@media (prefers-color-scheme: dark) { ${'a{} '.repeat(5000)}</style>`
  const start = performance.now()
  const result = prepareMailDocument(raw, false, 'Unavailable', true).document
  expect(performance.now() - start).toBeLessThan(1000)
  expect(result).not.toContain('a{}')
  const fake = '<!-- <style>@media (prefers-color-scheme:dark){.bad{color:red}}</style> --><p>Test</p>'
  expect(prepareMailDocument(fake, false, 'Unavailable', true).document).not.toContain('.bad{')
  const attr = '<p title="<style>@media (prefers-color-scheme:dark){.bad{color:red}}</style>">Test</p>'
  expect(prepareMailDocument(attr, false, 'Unavailable', true).document).not.toContain('.bad{color:red !important}')
})

test('links remain legible on a preserved mid-dark background', () => {
  const html = '<div style="background-color:#666666"><a href="https://example.test">Link</a></div>'
  const doc = prepareMailDocument(html, false, 'Unavailable', true).document
  expect(doc).toContain('color: rgb(232, 239, 236)')
})
