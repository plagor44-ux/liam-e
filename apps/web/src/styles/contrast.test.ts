import { expect, test } from 'bun:test'
import { readFileSync } from 'node:fs'

const css = readFileSync(new URL('./tokens.css', import.meta.url), 'utf8')
const pairs: [string, string[], number][] = [
  ['ink', ['surface', 'surface-soft', 'surface-ground', 'surface-overlay', 'accent-soft'], 4.5],
  ['ink-muted', ['surface', 'surface-soft', 'surface-ground', 'accent-soft'], 4.5],
  ['accent', ['surface', 'surface-soft', 'accent-soft'], 4.5],
  ['on-accent', ['accent', 'accent-hover'], 4.5],
  ['warm', ['surface', 'surface-soft', 'warm-soft'], 4.5],
  ['danger', ['surface', 'surface-soft', 'danger-soft'], 4.5],
  ['on-danger', ['danger'], 4.5],
  ['on-acc', ['acc-1', 'acc-2', 'acc-3', 'acc-4'], 4.5],
  ['tip-ink', ['tip-bg'], 4.5],
  ['control-border', ['surface', 'surface-soft'], 3],
  ['focus-ring', ['surface', 'surface-soft', 'surface-ground', 'accent-soft'], 3],
]
const destinationColors = ['moss', 'olive', 'teal', 'lake', 'denim', 'indigo', 'plum', 'berry']
for (const color of destinationColors) {
  pairs.push(['ink', [`filing-${color}-background`], 4.5])
  pairs.push([`filing-${color}-border`, ['surface', 'surface-soft'], 3])
}
function declarations(selector: RegExp): Record<string, string> {
  const block = css.match(selector)?.[1]
  if (!block) throw new Error(`Missing theme: ${selector}`)
  return Object.fromEntries([...block.matchAll(/--([\w-]+):\s*([^;]+);/g)].map((m) => [m[1]!, m[2]!.trim()]))
}
function luminance(value: string): number {
  if (!/^#[\da-f]{6}$/i.test(value)) throw new Error(`Not a hex colour: ${value}`)
  const channels = [1, 3, 5].map((i) => parseInt(value.slice(i, i + 2), 16) / 255)
  const linear = channels.map((v) => v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)
  return linear[0]! * 0.2126 + linear[1]! * 0.7152 + linear[2]! * 0.0722
}
for (const [name, selector] of [
  ['light', /:root,\s*\[data-theme="light"\]\s*\{([^}]+)}/],
  ['dark', /:root\[data-theme="dark"\][^{]*\{([^}]+)}/],
  ['system dark', /:root:not\(\[data-theme="light"\]\)\s*\{([^}]+)}/],
] as const) {
  test(`${name} theme contrast`, () => {
    const tokens = declarations(selector)
    expect(tokens.scrim).toBeTruthy()
    const resolve = (key: string): string => {
      const value = tokens[key]
      if (!value) throw new Error(`Missing ${key}`)
      const reference = value.match(/^var\(--([\w-]+)\)$/)
      return reference ? resolve(reference[1]!) : value
    }
    for (const color of destinationColors) for (const reserved of ['acc-1', 'acc-2', 'acc-3', 'acc-4', 'warm', 'danger']) {
      expect(resolve(`filing-${color}-border`), `${color} must not reuse ${reserved}`).not.toBe(resolve(reserved))
    }
    for (const [foreground, backgrounds, minimum] of pairs) for (const background of backgrounds) {
      const a = luminance(resolve(foreground)), b = luminance(resolve(background))
      const ratio = (Math.max(a, b) + .05) / (Math.min(a, b) + .05)
      expect(ratio, `${foreground} on ${background}`).toBeGreaterThanOrEqual(minimum)
    }
  })
}
