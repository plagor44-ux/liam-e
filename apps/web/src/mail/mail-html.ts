import createDOMPurify from 'dompurify'

// The DOM is detached and never inserted into the app; the result is only used inside
// an opaque-origin, scriptless sandbox with its own restrictive CSP.
export function prepareMailDocument(raw: string, images: boolean, placeholder: string): { document: string; hasRemoteImages: boolean } {
  const purifier = createDOMPurify(window)
  let hasRemoteImages = false
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
    ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'width', 'height', 'colspan', 'rowspan', 'style', 'target', 'rel'],
    ALLOW_DATA_ATTR: false,
  })
  const imagePolicy = images ? 'https: http:' : "'none'"
  return { document: `<!doctype html><html><head><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${imagePolicy}; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-src 'none'; object-src 'none'"><meta name="referrer" content="no-referrer"><style>html,body{margin:0;background:#fff;color:#171b1a;overflow-wrap:anywhere}body{padding:8px;font:16px/1.5 sans-serif}img{max-width:100%;height:auto}table{max-width:100%}</style></head><body>${clean}</body></html>`, hasRemoteImages }
}

export function buildMailDocument(raw: string, images: boolean, placeholder: string): string {
  return prepareMailDocument(raw, images, placeholder).document
}
