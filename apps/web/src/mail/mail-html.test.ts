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
