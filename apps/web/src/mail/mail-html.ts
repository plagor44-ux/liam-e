import createDOMPurify from 'dompurify'

type RGB = [number, number, number]
const darkSurface: RGB = [34, 45, 43]
const darkInk: RGB = [232, 239, 236]
const lightInk: RGB = [27, 36, 34]

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

// Only simple selectors and literal colour declarations survive; arbitrary sender CSS
// never reaches the frame. In particular URL-bearing declarations and at-rules are dropped.
function darkRules(raw: string): string {
  const rules: string[] = []
  for (const style of raw.matchAll(/<style\b[^>]*>([\s\S]*?)<\/style\s*>/giu)) {
    for (const media of style[1]!.matchAll(/@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)\s*\{((?:[^{}]*\{[^{}]*\}\s*)*)\}/giu)) {
      for (const rule of media[1]!.matchAll(/([^{}]+)\{([^{}]*)\}/gu)) {
        const selector = rule[1]!.trim()
        if (!/^[a-z0-9_.*#\-,\s>]+$/iu.test(selector)) continue
        const declarations = rule[2]!.split(';').flatMap(declaration => {
          const match = /^\s*(color|background|background-color)\s*:\s*([^;!]+)(?:!important)?\s*$/iu.exec(declaration)
          if (!match || !rgb(match[2]!)) return []
          return [`${match[1]!.toLowerCase() === 'background' ? 'background-color' : match[1]!.toLowerCase()}:${match[2]!.trim()} !important`]
        })
        const background = declarations.find(value => value.startsWith('background-color:'))?.split(':')[1]?.replace(' !important', '')
        const foreground = declarations.find(value => value.startsWith('color:'))?.split(':')[1]?.replace(' !important', '')
        if (background && foreground) {
          const bg = rgb(background), fg = rgb(foreground)
          if (bg && fg && contrast(bg, fg) < 4.5) {
            const readable = readableInk(bg)
            const index = declarations.findIndex(value => value.startsWith('color:'))
            declarations[index] = `color:${hexColor(readable)} !important`
          }
        } else if (background) {
          const bg = rgb(background)
          if (bg && contrast(bg, darkInk) < 4.5) declarations.push(`color:${hexColor(readableInk(bg))} !important`)
        } else if (foreground) {
          const fg = rgb(foreground)
          if (fg && contrast(darkSurface, fg) < 4.5) {
            const index = declarations.findIndex(value => value.startsWith('color:'))
            declarations[index] = `color:${hexColor(readableInk(darkSurface))} !important`
          }
        }
        if (declarations.length) rules.push(`${selector}{${declarations.join(';')}}`)
      }
    }
  }
  return rules.join('')
}

// The DOM is detached and never inserted into the app; the result is only used inside
// an opaque-origin, scriptless sandbox with its own restrictive CSP.
export function prepareMailDocument(raw: string, images: boolean, placeholder: string, dark = false): { document: string; hasRemoteImages: boolean } {
  const purifier = createDOMPurify(window)
  let hasRemoteImages = false
  const backgrounds = new WeakMap<Node, RGB>()
  const foregrounds = new WeakMap<Node, RGB>()
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
    if (dark && node instanceof window.HTMLElement) {
      const parent = node.parentNode ? backgrounds.get(node.parentNode) : undefined
      const backgroundValue = node.style.backgroundColor
      const background = rgb(backgroundValue) ?? (backgroundValue && backgroundValue !== 'transparent' ? darkSurface : null)
      const effective = background ? (luminance(background) > 0.35 ? darkSurface : background) : (parent ?? darkSurface)
      backgrounds.set(node, effective)
      if (background && (luminance(background) > 0.35 || !rgb(backgroundValue))) node.style.backgroundColor = hexColor(effective)
      const foreground = rgb(node.style.color) ?? (node.parentNode ? foregrounds.get(node.parentNode) : undefined) ?? darkInk
      const readable = contrast(foreground, effective) >= 4.5 ? foreground : readableInk(effective)
      foregrounds.set(node, readable)
      if (readable !== foreground || (node.style.color && !rgb(node.style.color))) node.style.color = hexColor(readable)
    }
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
  })
  const imagePolicy = images ? 'https: http:' : "'none'"
  const colors = dark ? `color-scheme:dark;background:#161e1c;color:#e8efec` : 'color-scheme:light;background:#fff;color:#171b1a'
  return { document: `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${imagePolicy}; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-src 'none'; object-src 'none'"><meta name="referrer" content="no-referrer"><style>html,body{margin:0;${colors};overflow-wrap:anywhere}body{padding:8px;font:16px/1.5 sans-serif}img{max-width:100%;height:auto}table{max-width:100%}${dark ? `a{color:#7ad3c3}${darkRules(raw)}` : ''}</style></head><body>${clean}</body></html>`, hasRemoteImages }
}

export function buildMailDocument(raw: string, images: boolean, placeholder: string): string {
  return prepareMailDocument(raw, images, placeholder).document
}
