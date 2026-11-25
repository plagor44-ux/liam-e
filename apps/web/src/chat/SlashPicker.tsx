// i18n-exempt: ARIA combobox/listbox attributes and CSS classes.
import { useEffect, useId, useLayoutEffect, useMemo, useRef, type CSSProperties, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { Button, Icon, IconButton } from '../ds'
import { useFloating } from '../ds/floating'
import { useI18n } from '../i18n/I18n'
import type { SlashOption } from './skill-selection'
import './composer-skills.css'

export function SlashPicker({
  open,
  id,
  anchorRef,
  query,
  options,
  activeIndex,
  onActiveIndexChange,
  loading,
  skillError,
  templateError,
  onRetry,
  onSelect,
  onClose,
  optionId,
}: {
  open: boolean
  id: string
  anchorRef: RefObject<HTMLElement | null>
  query: string
  options: SlashOption[]
  activeIndex: number
  onActiveIndexChange: (index: number) => void
  loading: boolean
  skillError: boolean
  templateError: boolean
  onRetry: () => void
  onSelect: (option: SlashOption) => void
  onClose: () => void
  optionId: (option: SlashOption) => string
}) {
  const { texts } = useI18n()
  const menuRef = useRef<HTMLDivElement>(null)
  const headingBaseId = useId()
  const error = skillError || templateError

  const spot = useFloating(
    open,
    anchorRef,
    menuRef,
    'top',
    'start',
    onClose,
    `${query}:${options.length}:${loading}:${error}`,
  )

  const placed = spot !== null
  const activeOption = options[activeIndex]
  const activeId = activeOption && optionId(activeOption)
  useLayoutEffect(() => {
    if (open && placed && activeId) {
      document.getElementById(activeId)?.scrollIntoView?.({ block: 'nearest', inline: 'nearest' })
    }
  }, [open, placed, activeId])

  const position = {
    left: spot?.left ?? 0,
    top: spot?.top ?? 0,
    visibility: placed ? 'visible' : 'hidden',
    '--le-menu-room': spot ? `${spot.maxHeight}px` : undefined,
  } as CSSProperties

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null
      if (!menuRef.current?.contains(target) && !anchorRef.current?.contains(target)) {
        onClose()
      }
    }
    const onOtherMenuOpen = () => onClose()
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('liam-menu-open', onOtherMenuOpen)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('liam-menu-open', onOtherMenuOpen)
    }
  }, [open, anchorRef, onClose])

  const grouped = useMemo(() => {
    const groups: {
      key: SlashOption['group']
      heading?: string
      items: { option: SlashOption; index: number }[]
    }[] = []
    const order: SlashOption['group'][] = ['skills', 'templates', 'revisions', 'actions']
    const headings: Record<SlashOption['group'], string | undefined> = {
      skills: texts.skills.slashHeading,
      templates: texts.skills.slashTemplatesHeading,
      revisions: texts.skills.slashRevisionsHeading,
      actions: undefined,
    }
    let flatIndex = 0
    for (const key of order) {
      const matched = options.filter(item => item.group === key)
      if (matched.length === 0) continue
      const items = matched.map(option => {
        const entry = { option, index: flatIndex }
        flatIndex += 1
        return entry
      })
      groups.push({ key, heading: headings[key], items })
    }
    return groups
  }, [options, texts])

  if (!open) return null

  const trimmed = query.trim()
  const hasCatalogEntries = options.some(item => item.kind === 'skill' || item.kind === 'template')

  return createPortal(
    <div
      ref={menuRef}
      className="le-menu le-menu-wide le-rise"
      style={position}
      onMouseDown={event => event.preventDefault()}
    >
      <div className="le-menu-head">
        <span>{trimmed ? texts.skills.slashFilter(trimmed) : texts.skills.slashMenuLabel}</span>
        <IconButton icon="x" label={texts.shell.close} onClick={onClose} />
      </div>

      <div id={id} role="listbox" aria-label={texts.skills.slashMenuLabel} className="le-menu-scroll">
        {loading && !hasCatalogEntries && (
          <div role="status" className="le-menu-empty">{texts.skills.loading}</div>
        )}

        {error && (
          <div role="alert" className="le-menu-empty">
            {skillError && <span>{texts.skills.loadError}</span>}
            {templateError && <span>{texts.prompts.error}</span>}
            <Button variant="ghost" onClick={onRetry}>{texts.chat.retry}</Button>
          </div>
        )}

        {!loading && !error && !hasCatalogEntries && !trimmed && (
          <div className="le-menu-empty">{texts.skills.slashEmptyCatalog}</div>
        )}

        {!loading && !error && options.length === 0 && trimmed.length > 0 && (
          <div className="le-menu-empty">
            <span>{texts.skills.slashNoMatch(trimmed)}</span>
            <span>{texts.skills.slashUnknownHint}</span>
          </div>
        )}

        {grouped.map(group => {
          const groupHeadingId = group.heading ? `${headingBaseId}-${group.key}` : undefined
          return (
            <div key={group.key} role="group" aria-labelledby={groupHeadingId}>
              {group.heading && (
                <div id={groupHeadingId} role="presentation" className="le-menu-section">
                  {group.heading}
                </div>
              )}
              <div className="le-menu-list">
                {group.items.map(({ option, index }) => {
                  const isActive = index === activeIndex
                  return (
                    <div key={option.key} className="le-menu-row" data-active={isActive || undefined}>
                      <button
                        type="button"
                        id={optionId(option)}
                        role="option"
                        aria-selected={isActive}
                        aria-disabled={option.disabled || undefined}
                        className="le-menu-row-main"
                        onMouseEnter={() => onActiveIndexChange(index)}
                        onClick={() => {
                          if (option.disabled) return
                          onSelect(option)
                        }}
                      >
                        <span className="le-menu-row-top">
                          <span className="le-menu-row-title">{option.label}</span>
                          {option.kind === 'skill' && (
                            <span className="le-menu-command">{option.command}</span>
                          )}
                          {(option.kind === 'skill' || option.kind === 'template') && option.badge && (
                            <span className="le-menu-badge">{option.badge}</span>
                          )}
                          {option.checked && <Icon name="check" size="sm" />}
                        </span>
                        {option.hint && <span className={option.group === 'actions' ? 'le-menu-meta le-menu-meta--wrap' : 'le-menu-meta'}>{option.hint}</span>}
                      </button>
                    </div>
                  )
                })}
              </div>
            </div>
          )
        })}
      </div>
    </div>,
    document.body,
  )
}
