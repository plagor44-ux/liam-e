import { useEffect, useLayoutEffect, useRef, useState, type AriaAttributes, type KeyboardEvent, type ReactNode, type RefObject } from 'react'
import { useI18n } from '../i18n/I18n'
import { Icon } from './Icon'
import { Tooltip } from './Tooltip'

type TextareaAccessibility = Pick<AriaAttributes, 'aria-autocomplete' | 'aria-controls' | 'aria-activedescendant' | 'aria-expanded'> & { role?: string }

export function Composer({
  value, onChange, onSubmit, onStop, busy, placeholder, autoFocus, controls, audioControls, locked = false,
  topContext, containerRef, textareaRef, onTextareaKeyDown, onCursorChange, textareaProps,
}: {
  value: string
  onChange: (value: string) => void
  onSubmit: () => void
  onStop: () => void
  busy: boolean
  placeholder: string
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
  const secondary = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!showControls) return
    const outside = (event: PointerEvent) => { if (!secondary.current?.contains(event.target as Node) && !(event.target as Element).closest?.('.le-menu')) setShowControls(false) }
    const escape = (event: globalThis.KeyboardEvent) => { if (event.key === 'Escape') setShowControls(false) }
    document.addEventListener('pointerdown', outside); document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('pointerdown', outside); document.removeEventListener('keydown', escape) }
  }, [showControls])
  const localTextarea = useRef<HTMLTextAreaElement>(null)
  const assignTextarea = (node: HTMLTextAreaElement | null) => {
    localTextarea.current = node
    if (textareaRef) textareaRef.current = node
  }
  useLayoutEffect(() => { const el = localTextarea.current; if (el) { el.style.height = 'auto'; el.style.height = `${Math.min(el.scrollHeight, 200)}px` } }, [value, compact])
  const send = () => { if (!busy && !locked && value.trim()) onSubmit() }
  return <div ref={containerRef} className="le-composer">{topContext}<textarea ref={assignTextarea} {...textareaProps} autoFocus={autoFocus} rows={1} aria-label={placeholder} placeholder={compact ? texts.controls.composerPlaceholder : placeholder} value={value} onChange={event => { onChange(event.target.value); onCursorChange?.(event.target.selectionStart ?? event.target.value.length) }} onClick={event => onCursorChange?.(event.currentTarget.selectionStart ?? value.length)} onKeyUp={event => onCursorChange?.(event.currentTarget.selectionStart ?? value.length)} onSelect={event => onCursorChange?.(event.currentTarget.selectionStart ?? value.length)} onKeyDown={event => { onTextareaKeyDown?.(event); if (event.defaultPrevented) return; if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send() } }} /><div className="le-composer-bar"><div className="le-composer-options" ref={secondary}><button type="button" className="le-composer-options-toggle" aria-label={texts.controls.composerOptions} aria-expanded={showControls} onClick={() => setShowControls(!showControls)}><Icon name="plus" /></button><div className={`le-composer-options-items${showControls ? ' is-open' : ''}`}>{controls}</div></div><span className="le-composer-spacer" />{audioControls}{busy ? <Tooltip content={texts.controls.stop}><button type="button" className="le-send le-send--stop" aria-label={texts.controls.stop} onClick={onStop}><Icon name="square" /></button></Tooltip> : <Tooltip content={texts.controls.sendTip}><button type="button" className="le-send" aria-label={texts.controls.send} disabled={locked || !value.trim()} onClick={send}><Icon name="arrow-up" /></button></Tooltip>}</div></div>
}
