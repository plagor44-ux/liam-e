// i18n-exempt: ARIA attributes, storage hook integration and CSS class names.
import { useEffect, useId, useMemo, useRef, useState, type CSSProperties } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import type { SkillOut } from '../api/generated/types.gen'
import { skillsQuery } from '../api/queries'
import { Button, Icon, IconButton, Tooltip } from '../ds'
import { useFloating } from '../ds/floating'
import { useI18n } from '../i18n/I18n'
import { isSkillCreator, skillDescription, skillName } from './skill-selection'
import './composer-skills.css'

export function SkillFavoritesMenu({
  disabled = false,
  favoriteIds,
  selectedSkillId,
  onSelectSkill,
  onToggleFavorite,
  onOpenSlash,
}: {
  disabled?: boolean
  favoriteIds: string[]
  selectedSkillId?: string | null
  onSelectSkill: (skill: SkillOut) => void
  onToggleFavorite: (skillId: string) => void
  onOpenSlash: () => void
}) {
  const { texts } = useI18n()
  const [open, setOpen] = useState(false)
  const [managing, setManaging] = useState(false)
  const anchorRef = useRef<HTMLSpanElement>(null)
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const menuId = useId()
  const list = useQuery(skillsQuery())

  // The creator starts only from its slash action, never as a favorite.
  const skills = useMemo(() => (list.data ?? []).filter(skill => !isSkillCreator(skill)), [list.data])
  const byId = useMemo(() => new Map(skills.map(skill => [skill.id, skill])), [skills])

  const favoriteSkills = useMemo(
    () => favoriteIds.map(id => byId.get(id)).filter((item): item is SkillOut => Boolean(item)),
    [favoriteIds, byId],
  )

  const sortedAllSkills = useMemo(
    () => [...skills].sort((a, b) => {
      const statusDiff = (a.status === 'active' ? 0 : 1) - (b.status === 'active' ? 0 : 1)
      if (statusDiff !== 0) return statusDiff
      return skillName(a, texts).localeCompare(skillName(b, texts))
    }),
    [skills, texts],
  )

  const close = (restoreFocus = true) => {
    setOpen(false)
    setManaging(false)
    if (restoreFocus) triggerRef.current?.focus({ preventScroll: true })
  }

  const spot = useFloating(
    open,
    anchorRef,
    menuRef,
    'top',
    'start',
    () => {
      if (menuRef.current?.contains(document.activeElement)) close(true)
      else close(false)
    },
    `${managing}:${favoriteSkills.length}:${skills.length}:${list.isPending}:${list.isError}`,
  )

  const placed = spot !== null
  const position = {
    left: spot?.left ?? 0,
    top: spot?.top ?? 0,
    visibility: placed ? 'visible' : 'hidden',
    '--le-menu-room': spot ? `${spot.maxHeight}px` : undefined,
  } as CSSProperties

  useEffect(() => {
    if (!open || !placed) return
    if (menuRef.current?.contains(document.activeElement)) return
    const first = menuRef.current?.querySelector<HTMLElement>('.le-menu-row-main:not(:disabled)')
      ?? menuRef.current?.querySelector<HTMLElement>('button:not(:disabled)')
    first?.focus()
  }, [open, placed])

  useEffect(() => {
    if (!open) return
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target as Node | null
      if (!menuRef.current?.contains(target) && !triggerRef.current?.contains(target)) {
        close(false)
      }
    }
    const onOtherMenuOpen = () => close(false)
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('liam-menu-open', onOtherMenuOpen)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('liam-menu-open', onOtherMenuOpen)
    }
  }, [open])

  return (
    <span ref={anchorRef} className="le-menu-anchor">
      <Tooltip content={texts.skills.favoritesTip}>
        <button
          ref={triggerRef}
          type="button"
          disabled={disabled}
          // The popup is a dialog with buttons, not a menu of menuitems (#163).
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-controls={open ? menuId : undefined}
          className="le-chip le-chip--quiet"
          onClick={() => {
            if (open) {
              close(false)
              return
            }
            document.dispatchEvent(new Event('liam-menu-open'))
            setManaging(false)
            setOpen(true)
          }}
        >
          <Icon name="plus" size="sm" />
          <span>{texts.skills.favoritesChip}</span>
        </button>
      </Tooltip>
      {open && createPortal(
        <div
          ref={menuRef}
          id={menuId}
          role="dialog"
          aria-label={texts.skills.favoritesMenuLabel}
          className="le-menu le-menu-wide le-rise"
          style={position}
          onKeyDown={event => {
            if (event.key === 'Escape') {
              event.preventDefault()
              close(true)
              return
            }
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
              const items = Array.from(menuRef.current?.querySelectorAll<HTMLElement>('.le-menu-row-main:not(:disabled)') ?? [])
              if (items.length === 0) return
              event.preventDefault()
              const idx = items.indexOf(document.activeElement as HTMLElement)
              const next = (idx + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length
              items[next]?.focus()
            }
          }}
        >
          <div className="le-menu-head">
            <span>{managing ? texts.skills.manageFavorites : texts.skills.favoritesHeading}</span>
            <IconButton icon="x" label={texts.shell.close} onClick={() => close(true)} />
          </div>

          <div className="le-menu-scroll">
            {list.isPending && skills.length === 0 && (
              <div role="status" className="le-menu-empty">{texts.skills.loading}</div>
            )}

            {list.isError && skills.length === 0 && (
              <div role="alert" className="le-menu-empty">
                <span>{texts.skills.loadError}</span>
                <Button variant="ghost" onClick={() => { void list.refetch() }}>{texts.chat.retry}</Button>
              </div>
            )}

            {!managing && favoriteSkills.length === 0 && !list.isPending && !list.isError && (
              <div className="le-menu-empty">
                <span>{texts.skills.favoritesEmpty}</span>
                <span>{texts.skills.favoritesEmptyHint}</span>
              </div>
            )}

            {!managing && favoriteSkills.length > 0 && (
              <div className="le-menu-list">
                {favoriteSkills.map(skill => {
                  const isDisabled = skill.status !== 'active'
                  const isSelected = selectedSkillId === skill.id
                  return (
                    <div key={skill.id} className="le-menu-row">
                      <button
                        type="button"
                        className="le-menu-row-main"
                        aria-disabled={isDisabled || undefined}
                        onClick={() => {
                          if (isDisabled) return
                          onSelectSkill(skill)
                          close(false)
                        }}
                      >
                        <span className="le-menu-row-top">
                          <span className="le-menu-row-title">{skillName(skill, texts)}</span>
                          <span className="le-menu-command">{`/${skill.slug}`}</span>
                          {isSelected && <Icon name="check" size="sm" />}
                        </span>
                        <span className="le-menu-meta">
                          {isDisabled ? texts.skills.disabledHint : skillDescription(skill, texts)}
                        </span>
                      </button>
                      <IconButton
                        icon="star"
                        className="le-fav-toggle"
                        aria-pressed={true}
                        label={texts.skills.favoriteRemove(skillName(skill, texts))}
                        onClick={() => onToggleFavorite(skill.id)}
                      />
                    </div>
                  )
                })}
              </div>
            )}

            {managing && (
              <div className="le-menu-list">
                {sortedAllSkills.map(skill => {
                  const isFav = favoriteIds.includes(skill.id)
                  const isDisabled = skill.status !== 'active' && !isFav
                  return (
                    <div key={skill.id} className="le-menu-row">
                      <button
                        type="button"
                        className="le-menu-row-main"
                        aria-disabled={isDisabled || undefined}
                        onClick={() => {
                          if (isDisabled) return
                          onToggleFavorite(skill.id)
                        }}
                      >
                        <span className="le-menu-row-top">
                          <span className="le-menu-row-title">{skillName(skill, texts)}</span>
                          <span className="le-menu-command">{`/${skill.slug}`}</span>
                          {isFav && <Icon name="star" size="sm" />}
                        </span>
                        <span className="le-menu-meta">
                          {skill.status !== 'active' ? texts.skills.disabledHint : skillDescription(skill, texts)}
                        </span>
                      </button>
                      <IconButton
                        icon="star"
                        className="le-fav-toggle"
                        aria-pressed={isFav}
                        disabled={isDisabled}
                        label={isFav ? texts.skills.favoriteRemove(skillName(skill, texts)) : texts.skills.favoriteAdd(skillName(skill, texts))}
                        onClick={() => {
                          if (!isDisabled) onToggleFavorite(skill.id)
                        }}
                      />
                    </div>
                  )
                })}
              </div>
            )}
          </div>

          <div className="le-menu-footer">
            <Button variant="ghost" aria-pressed={managing} onClick={() => setManaging(prev => !prev)}>
              {texts.skills.manageFavorites}
            </Button>
            <Button
              variant="ghost"
              onClick={() => {
                close(false)
                onOpenSlash()
              }}
            >
              {texts.skills.browseAll}
            </Button>
          </div>
        </div>,
        document.body,
      )}
    </span>
  )
}
