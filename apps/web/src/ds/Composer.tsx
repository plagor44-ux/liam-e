import { useEffect, useId, useLayoutEffect, useRef, useState, type AriaAttributes, type CSSProperties, type KeyboardEvent, type ReactNode, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { useFloating } from './floating'
import { useI18n } from '../i18n/I18n'
import { Icon } from './Icon'
import { Tooltip } from './Tooltip'

type TextareaAccessibility = Pick<AriaAttributes, 'aria-autocomplete' | 'aria-controls' | 'aria-activedescendant' | 'aria-expanded'> & { role?: string }

export function Composer({
  value, onChange, onSubmit, onStop, busy, placeholder, compactPlaceholder, autoFocus, controls, audioControls, locked = false,
  topContext, containerRef, textareaRef, onTextareaKeyDown, onCursorChange, textareaProps,
}: {
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
  onStop: () => void
  busy: boolean
  placeholder: string
  /** Shorter text for the narrow phone row; the full placeholder stays the field's name. */
  compactPlaceholder?: string
  autoFocus?: boolean
  controls?: ReactNode
  audioControls?: ReactNode
  locked?: boolean
  topContext?: ReactNode
  containerRef?: RefObject<HTMLDivElement | null>
  textareaRef?: RefObject<HTMLTextAreaElement | null>
  onTextareaKeyDown?: (event: KeyboardEvent<HTMLTextAreaElement>) => void
  onCursorChange?: (cursor: number) => void
  textareaProps?: TextareaAccessibility
}) {
  const { texts } = useI18n()
  const [showControls, setShowControls] = useState(false)
  const [compact, setCompact] = useState(false)
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const media = window.matchMedia('(max-width: 639px)')
    const update = () => setCompact(media.matches)
    update(); media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])
  useEffect(() => { if (!compact) setShowControls(false) }, [compact])
  const optionsButton = useRef<HTMLButtonElement>(null)
  const optionsPopup = useRef<HTMLDivElement>(null)
  const optionsFocused = useRef(false)
  const optionsId = useId()
  const closeOptions = (restore = false) => {
    setShowControls(false)
    if (restore) optionsButton.current?.focus({ preventScroll: true })
  }
  const spot = useFloating(compact && showControls, optionsButton, optionsPopup, 'top', 'start', () => closeOptions(optionsPopup.current?.contains(document.activeElement)))
  useEffect(() => {
    if (!showControls || !compact) { optionsFocused.current = false; return }
    if (!spot || optionsFocused.current) return
    optionsPopup.current?.querySelector<HTMLElement>('button:not([disabled])')?.focus({ preventScroll: true })
    optionsFocused.current = true
  }, [showControls, compact, spot])
  useEffect(() => {
    if (!showControls || !compact) return
    const outside = (event: PointerEvent) => {
      const target = event.target as Node
      const nested = Array.from(optionsPopup.current?.querySelectorAll('[aria-controls]') ?? []).some(element => {
        const id = element.getAttribute('aria-controls')
        return id !== null && document.getElementById(id)?.contains(target)
      })
      if (!optionsButton.current?.contains(target) && !optionsPopup.current?.contains(target) && !nested) closeOptions()
    }
    const escape = (event: globalThis.KeyboardEvent) => { if (event.key === 'Escape' && !event.defaultPrevented) closeOptions(true) }
    document.addEventListener('pointerdown', outside); document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape) }
  }, [showControls, compact])
  const popupStyle = { left: spot?.left ?? 0, top: spot?.top ?? 0, visibility: spot ? 'visible' : 'hidden', '--le-menu-room': spot ? `${spot.maxHeight}px` : undefined } as CSSProperties
  const localTextarea = useRef<HTMLTextAreaElement>(null)
  const assignTextarea = (node: HTMLTextAreaElement | null) => {
    localTextarea.current = node
    if (textareaRef) textareaRef.current = node
  }
  useLayoutEffect(() => { const el = localTextarea.current; if (el) { el.style.height = 'auto'; el.style.height = `${Math.min(el.scrollHeight, 200)}px` } }, [value, compact])
  const send = () => { if (!busy && !locked && value.trim()) onSubmit() }
  return <div ref={containerRef} className="le-composer">{topContext}<textarea ref={assignTextarea} {...textareaProps} autoFocus={autoFocus} rows={1} aria-label={placeholder} placeholder={compact ? compactPlaceholder ?? texts.controls.composerPlaceholder : placeholder} value={value} onChange={event => { onChange(event.target.value); onCursorChange?.(event.target.selectionStart ?? event.target.value.length) }} onClick={event => onCursorChange?.(event.currentTarget.selectionStart ?? value.length)} onKeyUp={event => onCursorChange?.(event.currentTarget.selectionStart ?? value.length)} onSelect={event => onCursorChange?.(event.currentTarget.selectionStart ?? value.length)} onKeyDown={event => { onTextareaKeyDown?.(event); if (event.defaultPrevented) return; if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send() } }} /><div className="le-composer-bar"><div className="le-composer-options"><button ref={optionsButton} type="button" className="le-composer-options-toggle" aria-label={texts.controls.composerOptions} aria-expanded={showControls} aria-controls={compact && showControls ? optionsId : undefined} onClick={() => setShowControls(!showControls)}><Icon name="plus" /></button>{!compact && <div className="le-composer-options-items">{controls}</div>}{compact && showControls && createPortal(<div id={optionsId} ref={optionsPopup} role="group" aria-label={texts.controls.composerOptions} className="le-menu le-composer-options-popup" style={popupStyle} onKeyDown={event => {
    if (event.key !== 'Tab') return
    const buttons = Array.from(optionsPopup.current?.querySelectorAll<HTMLButtonElement>('button:not([disabled])') ?? [])
    if (event.shiftKey && document.activeElement === buttons[0]) { event.preventDefault(); closeOptions(true) }
    else if (!event.shiftKey && document.activeElement === buttons.at(-1)) {
      event.preventDefault(); closeOptions()
      const next = optionsButton.current?.closest('.le-composer-bar')?.querySelector<HTMLElement>('.le-audio-controls button:not([disabled]), .le-send:not([disabled])') ?? optionsButton.current
      next?.focus({ preventScroll: true })
    }
  }}>{controls}</div>, document.body)}</div><span className="le-composer-spacer" />{audioControls}{busy ? <Tooltip content={texts.controls.stop}><button type="button" className="le-send le-send--stop" aria-label={texts.controls.stop} onClick={onStop}><Icon name="square" /></button></Tooltip> : <Tooltip content={texts.controls.sendTip}><button type="button" className="le-send" aria-label={texts.controls.send} disabled={locked || !value.trim()} onClick={send}><Icon name="arrow-up" /></button></Tooltip>}</div></div>
}
