import { useLayoutEffect, useRef } from 'react'
import { IconButton, Menu, Notice } from '../ds'
import { useI18n } from '../i18n/I18n'
import { useMailActionsContext } from './useMailActions'

// Feedback of the shared mail actions. The list renders it as an overlay; the reading view
// renders it inline when it stands alone (mobile and tablet), since the list is unmounted there.
export function ActionToast({ inline }: { inline?: boolean }) {
  const { texts } = useI18n()
  const { notice, permissionNotice, dismissPermissionNotice, undo, entries } = useMailActionsContext()
  const root = useRef<HTMLDivElement>(null)
  const lastFocus = useRef<HTMLElement | null>(null)
  useLayoutEffect(() => {
    const remember = (event: FocusEvent) => {
      const active = event.target as HTMLElement | null
      const menuId = root.current?.querySelector<HTMLElement>('[aria-controls]')?.getAttribute('aria-controls')
      lastFocus.current = active && (root.current?.contains(active) || (menuId && document.getElementById(menuId)?.contains(active))) ? active : null
    }
    document.addEventListener('focusin', remember)
    return () => document.removeEventListener('focusin', remember)
  }, [])
  useLayoutEffect(() => {
    if (lastFocus.current && !lastFocus.current.isConnected && document.activeElement === document.body) {
      const next = root.current?.querySelector<HTMLElement>('button:not([disabled])') ?? document.querySelector<HTMLElement>('.le-inbox-scroll, .le-mail-topbar button')
      lastFocus.current = null
      next?.focus({ preventScroll: true })
    }
  }, [entries.length, permissionNotice])
  const latest = entries.at(-1)
  if (!latest && !notice && !permissionNotice) return null
  const single = entries.length === 1 ? latest : undefined
  return <div ref={root} className={inline ? 'le-inbox-toast le-inbox-toast--inline' : 'le-inbox-toast'}>
    {latest && <Notice tone={latest.partial ? 'danger' : 'success'}>
      <span className={latest.wrap ? 'le-inbox-toast-text--wrap' : undefined}>{latest.text}</span>
      {single && <button type="button" className="le-link" disabled={single.pending} onClick={() => { void undo(single.actionId) }}>{texts.proposal.undo}</button>}
      {entries.length > 1 && <Menu placement="up-end"
        trigger={props => <button type="button" className="le-link" {...props}>{texts.inbox.recentActions(entries.length)}</button>}
        items={entries.map(entry => ({
          label: texts.inbox.undoAction(entry.label),
          disabled: entry.pending,
          hint: entry.pending ? texts.inbox.undoRunning : undefined,
          onSelect: () => { void undo(entry.actionId) },
        }))}
      />}
    </Notice>}
    {notice && <Notice tone={notice.tone}>
      <span className={notice.wrap ? 'le-inbox-toast-text--wrap' : undefined}>{notice.text}</span>
    </Notice>}
    {permissionNotice && <Notice tone="danger">
      <span className="le-inbox-toast-text--wrap">{permissionNotice}</span>
      <IconButton icon="x" label={texts.inbox.dismissNotice} onClick={dismissPermissionNotice} />
    </Notice>}
  </div>
}
