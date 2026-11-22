import { cloneElement, useEffect, useId, useRef, useState, type ReactElement, type HTMLAttributes } from 'react'
import { createPortal } from 'react-dom'
import { useFloating, type Align, type Side } from './floating'

// DESIGN.md §5: tooltips appear after 350 ms of hovering and at once on keyboard focus.
const HOVER_DELAY_MS = 350
const TOUCH_GUARD_MS = 1000
type Pos = 'top' | 'right' | 'bottom' | 'bottom-end' | 'top-end'
type TriggerProps = HTMLAttributes<HTMLElement> & { 'aria-describedby'?: string }
// Focus shows the tip only for keyboard focus; a script moving focus (e.g. into an opened drawer) or a touch tap does not.
let hadKeyboardEvent = false
let touchInteraction = false
let lastTouchTime = 0
let modalityBound = false

function markTouch() {
  hadKeyboardEvent = false
  touchInteraction = true
  lastTouchTime = Date.now()
}

function ensureModalityListeners() {
  if (modalityBound || typeof document === 'undefined') return
  modalityBound = true
  document.addEventListener('keydown', (event) => {
    if (!event.metaKey && !event.altKey && !event.ctrlKey) {
      hadKeyboardEvent = true
      touchInteraction = false
      lastTouchTime = 0
    }
  }, true)
  document.addEventListener('mousedown', () => {
    hadKeyboardEvent = false
  }, true)
  document.addEventListener('pointerdown', (event) => {
    hadKeyboardEvent = false
    const pointerType = (event as PointerEvent).pointerType
    if (pointerType === 'touch') markTouch()
    else if (pointerType === 'mouse' || pointerType === 'pen') {
      touchInteraction = false
      lastTouchTime = 0
    }
  }, true)
  document.addEventListener('mousemove', () => {
    if (touchInteraction && Date.now() - lastTouchTime > TOUCH_GUARD_MS) {
      touchInteraction = false
    }
  }, true)
  document.addEventListener('touchstart', () => {
    markTouch()
  }, true)
}

const isTouchActive = () => touchInteraction || (lastTouchTime > 0 && Date.now() - lastTouchTime <= TOUCH_GUARD_MS)

const keyboardFocus = (element: Element) => {
  if (!hadKeyboardEvent || isTouchActive()) return false
  try { return element.matches(':focus-visible') } catch { return true }
}

const PLACEMENT: Record<Pos, [Side, Align]> = { top: ['top', 'center'], 'top-end': ['top', 'end'], bottom: ['bottom', 'center'], 'bottom-end': ['bottom', 'end'], right: ['right', 'center'] }

// The bubble hangs in <body> with fixed coordinates from floating.ts; `pos` is only the preferred side.
export function Tooltip({ content, children, pos = 'top' }: { content: string; children: ReactElement<TriggerProps>; pos?: Pos }) {
  ensureModalityListeners()
  const [visible, setVisible] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const anchor = useRef<HTMLSpanElement>(null)
  const bubble = useRef<HTMLSpanElement>(null)
  const id = useId()
  const clear = () => { clearTimeout(timer.current); timer.current = undefined }
  const hide = () => { clear(); setVisible(false) }
  useEffect(() => {
    const onOverlayOpen = () => { clear(); setVisible(false) }
    document.addEventListener('liam-menu-open', onOverlayOpen)
    return () => {
      clearTimeout(timer.current)
      document.removeEventListener('liam-menu-open', onOverlayOpen)
    }
  }, [])
  useEffect(() => {
    if (!visible) return
    const dismiss = () => { clear(); setVisible(false) }
    document.addEventListener('pointerdown', dismiss, true)
    document.addEventListener('touchstart', dismiss, true)
    return () => {
      document.removeEventListener('pointerdown', dismiss, true)
      document.removeEventListener('touchstart', dismiss, true)
    }
  }, [visible])
  const [side, align] = PLACEMENT[pos]
  // A long text (a mail subject) that fits on neither side covers the trigger rather than leave the screen.
  const coords = useFloating(visible, anchor, bubble, side, align, () => setVisible(false), content, true)
  return <span ref={anchor} className="le-tooltip-anchor">{cloneElement(children, {
    'aria-describedby': [children.props['aria-describedby'], visible && id].filter(Boolean).join(' ') || undefined,
    onPointerDown: (event) => {
      children.props.onPointerDown?.(event)
      if ((event.nativeEvent as PointerEvent).pointerType === 'touch') markTouch()
      hide()
    },
    onTouchStart: (event) => {
      children.props.onTouchStart?.(event)
      markTouch()
      hide()
    },
    onClick: (event) => {
      children.props.onClick?.(event)
      hide()
    },
    onMouseEnter: (event) => {
      children.props.onMouseEnter?.(event)
      clear()
      if (isTouchActive()) return
      timer.current = setTimeout(() => {
        if (!isTouchActive()) setVisible(true)
      }, HOVER_DELAY_MS)
    },
    onMouseLeave: (event) => { children.props.onMouseLeave?.(event); hide() },
    onFocus: (event) => { children.props.onFocus?.(event); clear(); if (keyboardFocus(event.currentTarget)) setVisible(true) },
    onBlur: (event) => { children.props.onBlur?.(event); hide() },
  })}{visible && createPortal(
    <span ref={bubble} role="tooltip" id={id} className="le-tooltip" style={{ left: coords?.left ?? 0, top: coords?.top ?? 0, visibility: coords ? 'visible' : 'hidden' }}>{content}</span>,
    document.body,
  )}</span>
}
