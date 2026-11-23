import createDOMPurify from 'dompurify'

type RGB = [number, number, number]
const darkSurface: RGB = [34, 45, 43]
const darkInk: RGB = [232, 239, 236]
const lightInk: RGB = [27, 36, 34]
const linkInk: RGB = [122, 211, 195]

function rgb(value: string): RGB | null {
  const names: Record<string, string> = { white: '#ffffff', black: '#000000', transparent: '', red: '#ff0000', blue: '#0000ff', gray: '#808080', grey: '#808080' }
  value = value.trim().toLowerCase()
  value = names[value] ?? value
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/u.exec(value)?.[1]
  if (hex) {
    const full = hex.length === 3 ? [...hex].map(x => x + x).join('') : hex
    return [0, 2, 4].map(i => Number.parseInt(full.slice(i, i + 2), 16)) as RGB
  }
  const match = /^rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)$/u.exec(value)
  if (!match) return null
  const result = match.slice(1).map(Number)
  return result.every(n => n <= 255) ? result as RGB : null
}

function luminance(color: RGB): number {
  const linear = color.map(n => { const v = n / 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4 })
  return 0.2126 * linear[0]! + 0.7152 * linear[1]! + 0.0722 * linear[2]!
}

function contrast(a: RGB, b: RGB): number {
  const values = [luminance(a), luminance(b)].sort((x, y) => x - y)
  return (values[1]! + 0.05) / (values[0]! + 0.05)
}

const hexColor = (value: RGB) => `#${value.map(n => n.toString(16).padStart(2, '0')).join('')}`
function readableInk(background: RGB): RGB {
  const preferred = contrast(background, darkInk) >= contrast(background, lightInk) ? darkInk : lightInk
  if (contrast(background, preferred) >= 4.5) return preferred
  const white: RGB = [255, 255, 255], black: RGB = [0, 0, 0]
  return contrast(background, white) >= contrast(background, black) ? white : black
}

// Visit each character once, discarding incomplete blocks. Callers bound both input and output.
function blocks(input: string, limit: number): Array<[string, string]> {
  const result: Array<[string, string]> = []
  let start = 0, open = -1, depth = 0
  for (let i = 0; i < input.length && result.length < limit; i++) {
    if (input[i] === ';' && depth === 0) {
      start = i + 1
    } else if (input[i] === '{') {
      if (depth === 0) open = i
      depth++
    } else if (input[i] === '}' && depth > 0) {
      depth--
      if (depth === 0 && open >= 0) {
        result.push([input.slice(start, open).trim(), input.slice(open + 1, i)])
        start = i + 1
      }
    }
  }
  return result
}

// CSS comments and legacy HTML comment delimiters are discarded in one pass.
// URL-bearing declarations are still excluded from the rules subsequently accepted.
function stripComments(input: string): string {
  const output: string[] = []
  let i = 0, quote = ''
  while (i < input.length) {
    const c = input[i]!
    if (quote) {
      output.push(c)
      if (c === '\\' && i + 1 < input.length) output.push(input[++i]!)
      else if (c === quote) quote = ''
      i++
    } else if (c === '"' || c === "'") {
      quote = c
      output.push(c)
      i++
    } else if (input.startsWith('/*', i)) {
      const end = input.indexOf('*/', i + 2)
      if (end < 0) break
      output.push(' ')
      i = end + 2
    } else if (input.startsWith('<!--', i)) {
      output.push(' ')
      i += 4
    } else if (input.startsWith('-->', i)) {
      output.push(' ')
      i += 3
    } else {
      output.push(c)
      i++
    }
  }
  return output.join('')
}

// Only simple selectors and literal colour declarations survive; arbitrary sender CSS
// never reaches the frame. In particular URL-bearing declarations and at-rules are dropped.
type DarkRule = { selector: string; declarations: Array<[string, string]> }
function darkRules(stylesheets: string[]): DarkRule[] {
  const rules: DarkRule[] = []
  for (const style of stylesheets) {
    for (const [header, media] of blocks(stripComments(style), 128)) {
      if (!/^@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)$/iu.test(header)) continue
      for (const [selector, body] of blocks(media, 128 - rules.length)) {
        if (!/^[a-z0-9_.*#\-,\s>]+$/iu.test(selector)) continue
        const declarations: Array<[string, string]> = body.split(';').flatMap(declaration => {
          const colon = declaration.indexOf(':')
          if (colon < 0) return []
          const name = declaration.slice(0, colon).trim().toLowerCase()
          if (!['color', 'background', 'background-color'].includes(name)) return []
          let value = declaration.slice(colon + 1).trim()
          if (value.toLowerCase().endsWith('!important')) value = value.slice(0, -10).trim()
          if (!rgb(value)) return []
          return [[name === 'background' ? 'background-color' : name, value]]
        })
        if (declarations.length) rules.push({ selector, declarations })
      }
    }
  }
  return rules
}

function applyDarkRules(fragment: DocumentFragment, rules: DarkRule[]): void {
  const matched = new WeakMap<Element, Map<string, { value: string; weight: number }>>()
  const explicitBackground = new WeakSet<Element>()
  let selectorBudget = 128
  for (const rule of rules) {
    if (rule.selector.length > 512) continue
    for (const selector of rule.selector.split(',')) {
      if (selectorBudget-- <= 0) break
      const part = selector.trim().replace(/(^|[\s>])(html|body)(?=$|[.#\s>])/giu, '$1.le-mail-dark-root')
      const weight = (part.match(/#[\w-]+/gu)?.length ?? 0) * 1_000_000
        + (part.match(/\.[\w-]+/gu)?.length ?? 0) * 1_000
        + (part.match(/(?:^|[\s>])([a-z][\w-]*)/giu)?.length ?? 0)
      let elements: NodeListOf<Element>
      try { elements = fragment.querySelectorAll(part) } catch { continue }
      for (const node of elements) {
        const values = matched.get(node) ?? new Map<string, { value: string; weight: number }>()
        for (const [name, value] of rule.declarations) {
          const previous = values.get(name)
          if (!previous || weight >= previous.weight) values.set(name, { value, weight })
        }
        matched.set(node, values)
      }
    }
  }
  const backgrounds = new WeakMap<Node, RGB>(), foregrounds = new WeakMap<Node, RGB>()
  for (const node of fragment.querySelectorAll('*')) {
    if (!(node instanceof window.HTMLElement)) continue
    const values = matched.get(node)
    for (const [name, selected] of values ?? []) {
      node.style.setProperty(name, selected.value)
      if (name === 'background-color') explicitBackground.add(node)
    }
    const parent = node.parentNode ? backgrounds.get(node.parentNode) : undefined
    const backgroundValue = node.style.backgroundColor
    const background = rgb(backgroundValue) ?? (backgroundValue && backgroundValue !== 'transparent' ? darkSurface : null)
    const effective = background ? (!explicitBackground.has(node) && luminance(background) > 0.35 ? darkSurface : background) : (parent ?? darkSurface)
    backgrounds.set(node, effective)
    if (background && !explicitBackground.has(node) && (luminance(background) > 0.35 || !rgb(backgroundValue))) node.style.backgroundColor = hexColor(effective)
    const foreground = rgb(node.style.color) ?? (node.nodeName === 'A' ? linkInk : (node.parentNode ? foregrounds.get(node.parentNode) : undefined)) ?? darkInk
    const readable = contrast(foreground, effective) >= 4.5 ? foreground : readableInk(effective)
    foregrounds.set(node, readable)
    if (node.nodeName === 'A' || readable !== foreground || (node.style.color && !rgb(node.style.color))) node.style.color = hexColor(readable)
  }
}

// The DOM is detached and never inserted into the app; the result is only used inside
// an opaque-origin, scriptless sandbox with its own restrictive CSP.
export function prepareMailDocument(raw: string, images: boolean, placeholder: string, dark = false): { document: string; hasRemoteImages: boolean } {
  const purifier = createDOMPurify(window)
  let hasRemoteImages = false
  const stylesheets: string[] = []
  let cssBudget = 50_000
  if (dark) {
    // The detached sanitized fragment contains no image, frame, or link elements. Extract
    // actual style nodes, never matches from comments or attribute strings.
    const fragment = createDOMPurify(window).sanitize(raw.slice(0, 200_000), {
      ALLOWED_TAGS: ['style'], ALLOWED_ATTR: [], FORCE_BODY: true,
      RETURN_DOM_FRAGMENT: true, SAFE_FOR_XML: false,
    })
    for (const node of fragment.querySelectorAll('style')) {
      if (cssBudget <= 0 || stylesheets.length >= 32) break
      const css = (node.textContent ?? '').slice(0, cssBudget)
      stylesheets.push(css)
      cssBudget -= css.length
    }
  }
  const styles = new Set(['color', 'background-color', 'font-weight', 'font-style', 'font-size', 'text-align', 'text-decoration', 'padding', 'margin', 'border', 'border-color', 'border-width', 'border-style', 'width', 'max-width', 'height', 'vertical-align'])
  purifier.addHook('uponSanitizeAttribute', (node, data) => {
    const name = data.attrName.toLowerCase()
    if (name === 'style') {
      data.attrValue = data.attrValue.split(';').map(part => part.split(':')).filter(parts => parts.length === 2 && styles.has(parts[0]!.trim().toLowerCase()) && /^[#\w\s.,%()\-]+$/u.test(parts[1]!) && !/(url|expression|var|image|import)/iu.test(parts[1]!)).map(parts => `${parts[0]!.trim()}:${parts[1]!.trim()}`).join(';')
      if (!data.attrValue) data.keepAttr = false
    }
    if (name === 'href' || name === 'src') {
      try {
        const url = new URL(data.attrValue)
        if (name === 'src' && node.nodeName === 'IMG' && ['http:', 'https:'].includes(url.protocol)) hasRemoteImages = true
        if (!['http:', 'https:'].includes(url.protocol) || (name === 'src' && (!images || node.nodeName !== 'IMG'))) data.keepAttr = false
      } catch { data.keepAttr = false }
    }
  })
  purifier.addHook('afterSanitizeAttributes', node => {
    if (node.nodeName === 'A' && node.hasAttribute('href')) {
      node.setAttribute('target', '_blank')
      node.setAttribute('rel', 'noopener noreferrer')
    }
    if (node.nodeName === 'IMG' && !node.hasAttribute('src')) node.setAttribute('alt', placeholder)
  })
  const clean = purifier.sanitize(raw, {
    ALLOWED_TAGS: ['a', 'p', 'div', 'span', 'br', 'hr', 'b', 'strong', 'i', 'em', 'u', 's', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote', 'pre', 'code', 'ul', 'ol', 'li', 'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'img'],
    ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'width', 'height', 'colspan', 'rowspan', 'style', 'target', 'rel', 'class', 'id'],
    ALLOW_DATA_ATTR: false,
    RETURN_DOM_FRAGMENT: true,
  })
  if (dark) {
    const root = document.createElement('div')
    root.className = 'le-mail-dark-root'
    root.append(clean)
    clean.append(root)
    applyDarkRules(clean, darkRules(stylesheets))
  }
  const wrapper = document.createElement('template')
  wrapper.content.append(clean)
  const content = wrapper.innerHTML
  const imagePolicy = images ? 'https: http:' : "'none'"
  const colors = dark ? `color-scheme:dark;background:#161e1c;color:#e8efec` : 'color-scheme:light;background:#fff;color:#171b1a'
  return { document: `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${imagePolicy}; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-src 'none'; object-src 'none'"><meta name="referrer" content="no-referrer"><style>html,body{margin:0;${colors};overflow-wrap:anywhere}body{padding:8px;font:16px/1.5 sans-serif}img{max-width:100%;height:auto}table{max-width:100%}</style></head><body>${content}</body></html>`, hasRemoteImages }
}

export function buildMailDocument(raw: string, images: boolean, placeholder: string): string {
  return prepareMailDocument(raw, images, placeholder).document
}
