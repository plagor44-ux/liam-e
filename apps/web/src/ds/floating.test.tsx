import { afterEach, describe, expect, mock, spyOn, test } from 'bun:test'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { place } from './floating'
import { Menu } from './Menu'
import { Composer } from './Composer'

const VIEW = { width: 1000, height: 800 }
const MENU = { width: 290, height: 200 }
const original = { width: window.innerWidth, height: window.innerHeight }
const box = (left: number, top: number, width: number, height: number) => ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}) }) as DOMRect

const sizes = { offsetWidth: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth')!, offsetHeight: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight')! }
afterEach(() => {
  cleanup()
  mock.restore()
  for (const [key, descriptor] of Object.entries(sizes)) Object.defineProperty(HTMLElement.prototype, key, descriptor)
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: original.width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: original.height })
})

describe('place', () => {
  test('puts a menu below the start of its trigger when there is room', () => {
    expect(place(box(100, 100, 40, 40), MENU, 'bottom', 'start', VIEW)).toEqual({ left: 100, top: 148, maxHeight: 800 - 140 - 16 })
  })

  test('flips above a trigger at the bottom of the screen', () => {
    const spot = place(box(100, 740, 40, 40), MENU, 'bottom', 'start', VIEW)
    expect(spot.top).toBe(740 - 8 - MENU.height)
    expect(spot.top + MENU.height).toBeLessThanOrEqual(740)
  })

  test('with room on neither side it takes the roomier one and limits the height to that room', () => {
    const spot = place(box(100, 300, 40, 40), { width: 290, height: 700 }, 'bottom', 'start', VIEW)
    expect(spot).toEqual({ left: 100, top: 348, maxHeight: 800 - 340 - 16 })
  })

  test('shifts into the viewport at the right edge and aligns end edges on request', () => {
    expect(place(box(900, 100, 40, 40), MENU, 'bottom', 'start', VIEW).left).toBe(1000 - 8 - MENU.width)
    expect(place(box(500, 100, 40, 40), MENU, 'bottom', 'end', VIEW).left).toBe(540 - MENU.width)
    expect(place(box(0, 100, 40, 40), MENU, 'bottom', 'end', VIEW).left).toBe(8)
  })

  test('with shift, an element too tall for either side moves fully into view, over its trigger', () => {
    const tall = { width: 240, height: 500 }
    expect(place(box(100, 300, 40, 40), tall, 'bottom', 'center', VIEW).top + tall.height).toBeGreaterThan(VIEW.height)
    const spot = place(box(100, 300, 40, 40), tall, 'bottom', 'center', VIEW, true)
    expect(spot.top).toBe(VIEW.height - 8 - tall.height)
    // Taller than the screen: it starts at the top margin; CSS caps a tooltip at the screen height
    // minus both margins (checked below), so its bottom stays 8 px above the edge.
    expect(place(box(100, 300, 40, 40), { width: 240, height: 900 }, 'bottom', 'center', VIEW, true).top).toBe(8)
    // An element that fits keeps its place.
    expect(place(box(100, 300, 40, 40), MENU, 'bottom', 'center', VIEW, true).top).toBe(348)
  })

  test('negative case: a trigger with room keeps its preferred side above', () => {
    expect(place(box(100, 400, 40, 40), MENU, 'top', 'start', VIEW).top).toBe(400 - 8 - MENU.height)
  })

  // A close button in the lower notice of a toast whose upper notice holds an undo link.
  const TIP = { width: 150, height: 30 }
  const toast = box(16, 600, 968, 180)
  const close = box(940, 700, 32, 32)

  test('keeps its gap from a box it must clear, still aligned with the trigger', () => {
    const spot = place(close, TIP, 'top', 'end', VIEW, true, toast)
    expect(spot).toEqual({ left: 972 - TIP.width, top: 600 - 8 - TIP.height, maxHeight: 600 - 16 })
    expect(spot.top + TIP.height).toBeLessThanOrEqual(toast.top - 8)
  })

  test('flips below the box it must clear when there is no room above it', () => {
    const spot = place(box(940, 40, 32, 32), TIP, 'top', 'end', VIEW, true, box(16, 10, 968, 100))
    expect(spot.top).toBe(110 + 8)
  })

  test('negative case: without a box to clear it sits next to its trigger', () => {
    expect(place(close, TIP, 'top', 'end', VIEW, true).top).toBe(700 - 8 - TIP.height)
  })
})

// jsdom lays nothing out: answer with a fixed viewport, a fixed menu size and the given trigger box.
function layout(trigger: () => DOMRect) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: VIEW.width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: VIEW.height })
  // Floating elements are measured by layout size, triggers by their box.
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get(this: HTMLElement) { return this.getAttribute('role') === 'menu' ? MENU.width : 0 } })
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get(this: HTMLElement) { return this.getAttribute('role') === 'menu' ? MENU.height : 0 } })
  spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(() => trigger())
}

function open() {
  render(
    <div style={{ overflowX: 'auto', position: 'relative', zIndex: 3 }} className="le-inbox-filters">
      <Menu trigger={props => <button {...props}>Alle Konten</button>} items={[{ label: 'Alle Konten', onSelect: () => {} }, { label: 'privat@example.com', onSelect: () => {} }]} />
    </div>,
  )
  fireEvent.click(screen.getByRole('button', { name: 'Alle Konten' }))
  return screen.getByRole('menu') as HTMLElement
}

describe('Menu placement', () => {
  test('hangs in <body>, outside the scroll area and stacking context of its trigger, with fixed coordinates and its room', () => {
    layout(() => box(16, 54, 120, 36))
    const menu = open()
    expect(menu.parentElement).toBe(document.body)
    expect(menu.closest('.le-inbox-filters')).toBeNull()
    expect(screen.getByRole('button', { name: 'Alle Konten' }).getAttribute('aria-controls')).toBe(menu.id)
    expect(menu.style.visibility).toBe('visible')
    expect(Number.parseFloat(menu.style.top)).toBe(54 + 36 + 8)
    expect(Number.parseFloat(menu.style.left)).toBe(16)
    expect(menu.style.getPropertyValue('--le-menu-room')).toBe(`${800 - 90 - 16}px`)
  })

  test('opens above a trigger at the bottom of the screen, like the composer in a conversation', () => {
    layout(() => box(411, 700, 120, 36))
    const menu = open()
    expect(Number.parseFloat(menu.style.top)).toBe(700 - 8 - MENU.height)
  })

  test('moves the focus in only once it is placed and visible', () => {
    layout(() => box(16, 54, 120, 36))
    const visibility: string[] = []
    const focus = HTMLElement.prototype.focus
    spyOn(HTMLElement.prototype, 'focus').mockImplementation(function (this: HTMLElement, options?: FocusOptions) {
      const menu = this.closest<HTMLElement>('[role="menu"]')
      if (menu) visibility.push(menu.style.visibility)
      focus.call(this, options)
    })
    open()
    // Browsers ignore focus() on an element with visibility: hidden; jsdom does not, so the order is checked.
    expect(visibility).toEqual(['visible'])
    expect(document.activeElement?.textContent).toBe('Alle Konten')
    expect(document.activeElement?.getAttribute('role')).toBe('menuitem')
  })

  test('Tab closes it and hands the focus back to the trigger', () => {
    layout(() => box(16, 54, 120, 36))
    const menu = open()
    fireEvent.keyDown(menu, { key: 'Tab' })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Alle Konten' }))
  })

  test('closes when its trigger scrolls out of view and returns the focus it held to the trigger', () => {
    let trigger = box(16, 54, 120, 36)
    layout(() => trigger)
    open()
    trigger = box(16, -100, 120, 36)
    act(() => { window.dispatchEvent(new Event('scroll')) })
    expect(screen.queryByRole('menu')).toBeNull()
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Alle Konten' }))
  })

  test('is placed again when its size changes while open (items arrive later)', () => {
    let resized = () => {}
    const original = globalThis.ResizeObserver
    globalThis.ResizeObserver = class { constructor(callback: () => void) { resized = callback } observe() {} disconnect() {} } as unknown as typeof ResizeObserver
    try {
      layout(() => box(16, 500, 120, 36))
      const menu = open()
      expect(Number.parseFloat(menu.style.top)).toBe(500 + 36 + 8)  // 200 px fit below
      Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get(this: HTMLElement) { return this.getAttribute('role') === 'menu' ? 400 : 0 } })
      act(() => resized())
      expect(Number.parseFloat(menu.style.top)).toBe(500 - 8 - 400)  // 400 px do not: it flips
    } finally { globalThis.ResizeObserver = original }
  })
})

test('mobile composer options escape the mail-question scroll container and use floating placement', async () => {
  const originalMatchMedia = window.matchMedia
  window.matchMedia = (() => ({ matches: true, addEventListener() {}, removeEventListener() {} })) as unknown as typeof window.matchMedia
  try {
    layout(() => box(16, 700, 44, 44))
    Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get(this: HTMLElement) { return this.getAttribute('role') === 'group' ? MENU.width : 0 } })
    Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get(this: HTMLElement) { return this.getAttribute('role') === 'group' ? MENU.height : 0 } })
    const view = render(<div className="le-chat-view--mail-question" style={{ overflow: 'hidden' }}><Composer value="" onChange={() => {}} onSubmit={() => {}} onStop={() => {}} busy={false} placeholder="Frage" controls={<button>Favoriten</button>} /></div>)
    fireEvent.click(screen.getByRole('button', { name: 'Weitere Optionen' }))
    const popup = await screen.findByRole('group', { name: 'Weitere Optionen' })
    expect(popup.parentElement).toBe(document.body)
    expect(view.container.contains(popup)).toBe(false)
    expect(popup.style.visibility).toBe('visible')
    expect(Number.parseFloat(popup.style.top)).toBe(700 - 8 - MENU.height)
    expect(Number.parseFloat(popup.style.left)).toBe(16)
    expect(popup.style.getPropertyValue('--le-menu-room')).not.toBe('')
  } finally { window.matchMedia = originalMatchMedia }
})

// Floating elements get their place from floating.ts only. A CSS offset per context (the old
// `.le-mail-topbar .le-menu { left: … }`) is what let menus and tooltips run out of sight.
test('no CSS rule positions a menu or tooltip, or styles one through its context', () => {
  const floating = /\.le-(?:menu|tooltip)(?:--[\w-]+)?(?![\w-])/u
  for (const file of ['../styles/ds.css', '../styles/ds-app.css']) {
    const css = readFileSync(new URL(file, import.meta.url), 'utf8').replace(/\/\*[\s\S]*?\*\//gu, '')
    for (const [, selector = '', body = ''] of css.matchAll(/([^{}]+)\{([^{}]*)\}/gu)) {
      if (!floating.test(selector)) continue
      expect({ selector: selector.trim(), offsets: /(?:^|;)\s*(?:top|left|right|bottom|inset)\s*:/u.test(body) }).toEqual({ selector: selector.trim(), offsets: false })
      expect(body).not.toMatch(/position:\s*(?:absolute|relative|static)/u)
      // They hang in <body>: a rule that styles them through an ancestor (`.le-chat-header .le-menu`) never applies.
      for (const part of selector.split(',').map(item => item.trim()).filter(item => floating.test(item))) {
        expect({ part, throughAncestor: !/^\.le-(?:menu|tooltip)(?:--[\w-]+)?(?![\w-])/u.test(part) }).toEqual({ part, throughAncestor: false })
      }
    }
  }
  const app = readFileSync(new URL('../styles/ds-app.css', import.meta.url), 'utf8')
  expect(app).toMatch(/(?:^|\n)\.le-menu \{[^}]*position: fixed/u)
  expect(app).toMatch(/\.le-tooltip \{[^}]*position: fixed/u)
  expect(app).toMatch(/\.le-tooltip \{[^}]*max-height: calc\(100dvh - 2 \* var\(--space-2\)\); overflow: hidden/u)
})
