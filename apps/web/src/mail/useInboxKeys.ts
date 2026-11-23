import { useEffect } from 'react'
import { useBreakpoint } from '../shell/useBreakpoint'
import { shortcutForKey } from './inboxShortcuts'

type Handlers = { onNext(): void; onPrev(): void; onOpen(): void; onArchive(): void; onToggleRead(): void; onTrash(): void; onSelect?(): void; onEscape?(): void; onFolder?(): void; onLabel?(): void; onOverview?(): void }

export function useInboxKeys(handlers: Handlers) {
  const breakpoint = useBreakpoint()
  useEffect(() => {
    if (breakpoint !== 'desktop' && breakpoint !== 'wide') return
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey || event.isComposing || (event.shiftKey && event.key !== '?' && event.key !== '#')) return
      const target = event.target instanceof Element ? event.target : null
      if (target?.closest('input, textarea, select, [contenteditable], [role="menu"], [role="dialog"]')) return
      if (document.querySelector('[role="menu"]')) return
      // An open dialog blocks the list even when the focus is not inside it (its focused element was removed).
      if (document.querySelector('[aria-modal="true"]')) return
      if (event.key === 'Enter' && target?.closest('button, a')) return
      const name = shortcutForKey(event.key)
      const action = name && { next: handlers.onNext, previous: handlers.onPrev, open: handlers.onOpen, archive: handlers.onArchive, read: handlers.onToggleRead, trash: handlers.onTrash, select: handlers.onSelect, escape: handlers.onEscape, folder: handlers.onFolder, label: handlers.onLabel, overview: handlers.onOverview }[name]
      if (action) { event.preventDefault(); action() }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [breakpoint, handlers])
}
