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
function hue(value: string): number {
  const rgb = [1, 3, 5].map(i => parseInt(value.slice(i, i + 2), 16) / 255)
  const max = Math.max(...rgb), min = Math.min(...rgb), delta = max - min
  if (!delta) throw new Error(`Achromatic palette colour: ${value}`)
  const [r, g, b] = rgb as [number, number, number]
  const angle = max === r ? ((g - b) / delta) % 6 : max === g ? (b - r) / delta + 2 : (r - g) / delta + 4
  return (angle * 60 + 360) % 360
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
    for (const [index, first] of destinationColors.entries()) for (const second of destinationColors.slice(index + 1)) {
      const distance = Math.abs(hue(resolve(`filing-${first}-border`)) - hue(resolve(`filing-${second}-border`)))
      expect(Math.min(distance, 360 - distance), `${first} and ${second} hue separation`).toBeGreaterThanOrEqual(20)
    }
    for (const [foreground, backgrounds, minimum] of pairs) for (const background of backgrounds) {
      const a = luminance(resolve(foreground)), b = luminance(resolve(background))
      const ratio = (Math.max(a, b) + .05) / (Math.min(a, b) + .05)
      expect(ratio, `${foreground} on ${background}`).toBeGreaterThanOrEqual(minimum)
    }
  })
}


test('palette swatches use the filled colour token in both themes', () => {
  const styles = readFileSync(new URL('./ds-app.css', import.meta.url), 'utf8')
  for (const color of destinationColors) expect(styles).toContain(`--filing-swatch-fill: var(--filing-${color}-border)`)
  expect(styles).toContain('.le-filing-swatch[class*="le-destination-"] { background: var(--filing-swatch-fill); }')
})
