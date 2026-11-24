import { expect, test } from 'bun:test'
import { readdirSync } from 'node:fs'
import { join, relative } from 'node:path'

// DESIGN.md §6 and §7.2: controls are control-height (38 px) tall; the 44 px of control-height-lg
// is the touch target for a coarse pointer, which ds.css already gives every base control.
// A stylesheet that sets it without `pointer: coarse` makes one view taller than the rest.
const root = new URL('..', import.meta.url).pathname
const control = /\.le-(btn|chip|icon-btn|send|link)(?![\w-])/u
const large = /(?<![\w-])(?:min-)?height\s*:\s*var\(--control-height-lg\)/u
// The reader bottom bar is mobile-only. Quick filing has an explicit narrow-screen exception.
const allowed = new Set(['.le-mail-bottom .le-btn'])
const mobileAllowed = new Set(['.le-quick-actions .le-btn', '.le-menu--mailrow .le-menu-item'])

function files(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap(entry => entry.isDirectory() ? files(join(dir, entry.name)) : entry.name.endsWith('.css') ? [join(dir, entry.name)] : [])
}

/** Splits at top-level commas, ignoring those inside parentheses or brackets. */
function list(text: string) {
  const parts: string[] = []
  let depth = 0, start = 0
  for (let i = 0; i < text.length; i++) {
    const char = text[i]
    if (char === '(' || char === '[') depth++
    else if (char === ')' || char === ']') depth--
    else if (char === ',' && depth === 0) { parts.push(text.slice(start, i)); start = i + 1 }
  }
  return [...parts, text.slice(start)].map(part => part.trim().replace(/\s+/gu, ' ')).filter(Boolean)
}

/** The compound a selector styles: after the last top-level combinator, without :has() and :not() arguments. */
function subject(selector: string) {
  let depth = 0, start = 0
  for (let i = 0; i < selector.length; i++) {
    const char = selector[i]!
    if (char === '(' || char === '[') depth++
    else if (char === ')' || char === ']') depth--
    else if (depth === 0 && /[\s>+~]/u.test(char)) start = i + 1
  }
  return selector.slice(start).replace(/:(?:has|not)\((?:[^()]|\([^()]*\))*\)/gu, '')
}

/** True when one enclosing @media applies only to a coarse pointer: every query of its list requires it. A query with `not` counts as not coarse. */
function coarse(chain: string[]) {
  return chain.some(prelude => {
    const query = /^@media\s+(.+)$/su.exec(prelude)?.[1]
    return query !== undefined && list(query).every(part => /\(\s*pointer\s*:\s*coarse\s*\)/u.test(part) && !/(^|[\s(])not[\s(]/u.test(part))
  })
}

/** An exception is safe only when every alternative requires a narrow screen or coarse pointer. */
function mobileOrCoarse(chain: string[]) {
  return chain.some(prelude => {
    const query = /^@media\s+(.+)$/su.exec(prelude)?.[1]
    return query !== undefined && list(query).every(part => (/\(\s*max-width\s*:\s*639px\s*\)/u.test(part) || /\(\s*pointer\s*:\s*coarse\s*\)/u.test(part)) && !/(^|[\s(])not[\s(]/u.test(part))
  })
}

/** Rules giving a control the large height outside a coarse pointer; comments removed, nesting by braces. */
function offending(css: string) {
  const out: string[] = []
  const stack: string[] = []
  let buffer = ''
  for (const char of css.replace(/\/\*[\s\S]*?\*\//gu, '')) {
    if (char === '{') { stack.push(buffer.trim()); buffer = '' }
    else if (char === '}') {
      const prelude = stack.pop() ?? ''
      if (!prelude.startsWith('@') && large.test(buffer) && !coarse(stack)) out.push(...list(prelude).filter(part => control.test(subject(part)) && !allowed.has(part) && !(mobileAllowed.has(part) && mobileOrCoarse(stack))))
      buffer = ''
    } else buffer += char
  }
  return out
}

test('no stylesheet makes a control 44 px tall outside a coarse pointer', async () => {
  const found: string[] = []
  for (const file of files(root)) found.push(...offending(await Bun.file(file).text()).map(part => `${relative(root, file)}: ${part}`))
  expect(found).toEqual([])
})

const rule = (selector: string) => `${selector} { display: flex; min-height: var(--control-height-lg); }`
test.each([
  ['an unconditional control', rule('.le-settings .le-btn'), ['.le-settings .le-btn']],
  ['height instead of min-height, with a state', rule('.x .le-chip:hover').replace('min-height', 'height'), ['.x .le-chip:hover']],
  ['the control inside :is()', rule('.x :is(.le-btn, .y)'), ['.x :is(.le-btn, .y)']],
  ['one control of a selector list', rule('.x .y, .x .le-link'), ['.x .le-link']],
  ['a comment mentioning the pointer', `/* (pointer: coarse) */ ${rule('.x .le-send')}`, ['.x .le-send']],
  ['a media list that also matches a fine pointer', `@media (pointer: coarse), (pointer: fine) { ${rule('.x .le-btn')} }`, ['.x .le-btn']],
  ['a negated coarse query', `@media not all and (pointer: coarse) { ${rule('.x .le-btn')} }`, ['.x .le-btn']],
  ['a coarse pointer', `@media (pointer: coarse) { ${rule('.x .le-btn')} }`, []],
  ['a coarse pointer and width', `@media (pointer: coarse) and (max-width: 639px) { ${rule('.x .le-btn')} }`, []],
  ['a coarse pointer nested in another media rule', `@media (min-width: 640px) { @media (pointer: coarse) { ${rule('.x .le-icon-btn')} } }`, []],
  ['a container that has a control', rule('.panel:has(.le-btn)'), []],
  ['an element that is not a control', rule('.le-btn-group, .x:not(.le-chip)'), []],
  ['a max-height', '.x .le-btn { max-height: var(--control-height-lg); }', []],
  ['the allowed mobile bar', rule('.le-mail-bottom .le-btn'), []],
])('the guard handles %s', (_, css, expected) => {
  expect(offending(css)).toEqual(expected)
})


test('mobile quick filing and row menu actions keep 44 px targets', async () => {
  const css = await Bun.file(new URL('./ds-app.css', import.meta.url)).text()
  expect(css).toContain('@media (max-width: 639px), (pointer: coarse) { .le-quick-actions .le-btn, .le-menu--mailrow .le-menu-item { min-width: var(--control-height-lg); min-height: var(--control-height-lg); } }')
})

test('the mobile quick-action exception rejects an unconditional height', () => {
  expect(offending(rule('.le-quick-actions .le-btn'))).toEqual(['.le-quick-actions .le-btn'])
})

test('grouping a mail row menu keeps its base width and wraps the hint', async () => {
  const css = await Bun.file(new URL('./ds-app.css', import.meta.url)).text()
  expect(css).toContain('.le-menu--grouped:not(.le-menu--mailrow) { width: 24rem; }')
  expect(css).toContain('.le-menu--mailrow .le-menu-group-label { overflow-wrap: anywhere; }')
})
