import { afterEach, describe, expect, spyOn, test, mock } from 'bun:test'
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { readFileSync } from 'node:fs'
import { Tooltip } from './Tooltip'

const VIEW = { width: 1000, height: 800 }
const TIP = { width: 100, height: 30 }
const original = { width: window.innerWidth, height: window.innerHeight }

const sizes = { offsetWidth: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetWidth')!, offsetHeight: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'offsetHeight')! }
afterEach(() => {
  cleanup()
  mock.restore()
  for (const [key, descriptor] of Object.entries(sizes)) Object.defineProperty(HTMLElement.prototype, key, descriptor)
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: original.width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: original.height })
})

const box = (left: number, top: number, width: number, height: number) => ({ left, top, width, height, right: left + width, bottom: top + height, x: left, y: top, toJSON: () => ({}) }) as DOMRect

// jsdom lays nothing out: answer with a fixed viewport, a fixed bubble size and the given trigger box.
function layout(trigger: DOMRect) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: VIEW.width })
  Object.defineProperty(window, 'innerHeight', { configurable: true, value: VIEW.height })
  // Floating elements are measured by layout size, triggers by their box.
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', { configurable: true, get(this: HTMLElement) { return this.getAttribute('role') === 'tooltip' ? TIP.width : 0 } })
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get(this: HTMLElement) { return this.getAttribute('role') === 'tooltip' ? TIP.height : 0 } })
  spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(() => trigger)
}

function show(pos?: 'top' | 'right' | 'bottom' | 'bottom-end' | 'top-end') {
  const view = render(
    <div className="le-inbox-reading" style={{ overflowY: 'auto' }}>
      <Tooltip content="Markiert die Mail als gelesen." pos={pos}><button>Trigger</button></Tooltip>
    </div>,
  )
  fireEvent.keyDown(document.body, { key: 'Tab' })
  act(() => screen.getByRole('button').focus())
  return { view, tip: screen.getByRole('tooltip') as HTMLElement }
}
const px = (value: string) => Number.parseFloat(value)

describe('Tooltip placement', () => {
  test('hangs in <body>, outside its anchor and the scroll container around the trigger', () => {
    layout(box(400, 400, 40, 40))
    const { view, tip } = show()
    expect(tip.parentElement).toBe(document.body)
    expect(tip.closest('.le-tooltip-anchor')).toBeNull()
    expect(tip.closest('.le-inbox-reading')).toBeNull()
    expect(view.container.contains(tip)).toBe(false)
  })

  test('a long text that fits on neither side stays fully on screen, over its trigger', () => {
    // Tooltip texts are short (DESIGN.md §6), but a long one must still not leave the screen.
    // 104 px above, 184 px below, 200 px needed: below is roomier but too small.
    layout(box(400, 120, 40, 40))
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 360 })
    Object.defineProperty(HTMLElement.prototype, 'offsetHeight', { configurable: true, get(this: HTMLElement) { return this.getAttribute('role') === 'tooltip' ? 200 : 0 } })
    const { tip } = show('bottom')
    expect(px(tip.style.top)).toBeGreaterThanOrEqual(8)
    expect(px(tip.style.top) + 200).toBeLessThanOrEqual(360 - 8)
  })

  test('stays above the trigger when there is room', () => {
    layout(box(400, 400, 40, 40))
    const { tip } = show()
    expect(tip.style.visibility).toBe('visible')
    expect(px(tip.style.top)).toBe(400 - 8 - TIP.height)
    expect(px(tip.style.left)).toBe(420 - TIP.width / 2)
  })

  test('flips below the trigger at the top edge of the viewport', () => {
    layout(box(400, 4, 40, 40))
    const { tip } = show('top')
    expect(px(tip.style.top)).toBe(4 + 40 + 8)
  })

  test('flips above a bottom placement at the bottom edge of the viewport', () => {
    layout(box(400, VIEW.height - 44, 40, 40))
    const { tip } = show('bottom')
    expect(px(tip.style.top)).toBe(VIEW.height - 44 - 8 - TIP.height)
  })

  test('is clamped into the viewport at the right edge', () => {
    layout(box(VIEW.width - 30, 400, 30, 40))
    const { tip } = show('top')
    expect(px(tip.style.left) + TIP.width).toBeLessThanOrEqual(VIEW.width - 8)
    expect(px(tip.style.left)).toBe(VIEW.width - 8 - TIP.width)
  })

  test('is clamped at the left edge too', () => {
    layout(box(0, 400, 20, 40))
    const { tip } = show('top')
    expect(px(tip.style.left)).toBe(8)
  })

  test('a right placement without room on the right falls back to top or bottom', () => {
    layout(box(VIEW.width - 30, 400, 30, 40))
    const { tip } = show('right')
    expect(px(tip.style.top)).toBe(400 - 8 - TIP.height)
    expect(px(tip.style.left) + TIP.width).toBeLessThanOrEqual(VIEW.width - 8)
  })

  test('a right placement with room sits 8 px right of the trigger, centred and clamped vertically', () => {
    layout(box(100, 400, 40, 40))
    expect(px(show('right').tip.style.left)).toBe(148)
    cleanup()
    layout(box(100, 2, 40, 20))
    const { tip } = show('right')
    expect(px(tip.style.top)).toBe(8)
  })

  test('top-end aligns the right edges', () => {
    layout(box(400, 400, 40, 40))
    expect(px(show('top-end').tip.style.left)).toBe(440 - TIP.width)
  })

  test('follows the trigger on scroll and goes away when the trigger leaves the viewport', () => {
    const trigger = box(400, 400, 40, 40)
    layout(trigger)
    const { tip } = show()
    expect(px(tip.style.top)).toBe(362)
    Object.assign(trigger, { top: 300, bottom: 340 })
    act(() => { fireEvent.scroll(window) })
    expect(px(screen.getByRole('tooltip').style.top)).toBe(262)
    Object.assign(trigger, { top: -200, bottom: -160 })
    act(() => { fireEvent.scroll(window) })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
})

describe('Tooltip accessibility', () => {
  test('aria-describedby points to the tooltip while it is visible and is removed after hide', () => {
    layout(box(400, 400, 40, 40))
    render(<Tooltip content="Hilfe"><button>Trigger</button></Tooltip>)
    const button = screen.getByRole('button')
    expect(button.getAttribute('aria-describedby')).toBeNull()
    fireEvent.keyDown(document.body, { key: 'Tab' })
    act(() => button.focus())
    const tip = screen.getByRole('tooltip')
    expect(tip.id).not.toBe('')
    expect(button.getAttribute('aria-describedby')).toBe(tip.id)
    act(() => button.blur())
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(button.getAttribute('aria-describedby')).toBeNull()
  })

  test('keeps a describedby the trigger already had', () => {
    layout(box(400, 400, 40, 40))
    render(<Tooltip content="Hilfe"><button aria-describedby="other">Trigger</button></Tooltip>)
    const button = screen.getByRole('button')
    fireEvent.keyDown(document.body, { key: 'Tab' })
    act(() => button.focus())
    expect(button.getAttribute('aria-describedby')).toBe(`other ${screen.getByRole('tooltip').id}`)
  })

  test('is removed from <body> on unmount', () => {
    layout(box(400, 400, 40, 40))
    const { view } = show()
    expect(document.body.querySelectorAll('[role="tooltip"]')).toHaveLength(1)
    view.unmount()
    expect(document.body.querySelectorAll('[role="tooltip"]')).toHaveLength(0)
  })

  test('does not stick on touch tap or stay open when a menu or drawer opens', async () => {
    layout(box(16, 16, 44, 44))
    render(<Tooltip content="Open menu"><button>Open menu</button></Tooltip>)
    const button = screen.getByRole('button', { name: 'Open menu' })

    // Touch tap fires touchstart, emulated mouseenter, focus and click without mouseleave.
    fireEvent.touchStart(button)
    fireEvent.mouseEnter(button)
    act(() => button.focus())
    fireEvent.click(button)
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 400)) })
    expect(screen.queryByRole('tooltip')).toBeNull()

    // Restore keyboard interaction and verify that opening a menu/drawer dismisses any visible tooltip.
    act(() => button.blur())
    fireEvent.keyDown(document.body, { key: 'Tab' })
    act(() => button.focus())
    expect(screen.getByRole('tooltip').textContent).toBe('Open menu')
    act(() => { document.dispatchEvent(new Event('liam-menu-open')) })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })
})

test('the bubble is fixed, ignores the pointer and sits above the scrim of dialogs', () => {
  const css = readFileSync(new URL('../styles/ds-app.css', import.meta.url), 'utf8')
  const rule = (selector: string) => css.match(new RegExp(`^${selector.replace('.', '\\.')} \\{([^}]*)\\}`, 'mu'))?.[1] ?? ''
  const tooltip = rule('.le-tooltip')
  expect(tooltip).toContain('position: fixed')
  expect(tooltip).toContain('pointer-events: none')
  expect(tooltip).toContain('max-width: 240px')
  expect(css).not.toMatch(/\.le-tooltip--/u)
  const zIndex = (body: string) => Number(/z-index: (\d+)/u.exec(body)?.[1])
  expect(zIndex(tooltip)).toBeGreaterThan(zIndex(rule('.le-scrim')))
})
