// i18n-exempt: API keys and query keys.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useState } from 'react'
import { mailFilingAssign } from '../api/generated/sdk.gen'
import type { FilingIn, MessageListItemOut } from '../api/generated/types.gen'
import { unwrap } from '../api/unwrap'
import { Button, IconButton, Menu, Notice, Tooltip } from '../ds'
import { useI18n } from '../i18n/I18n'
import { foldersQuery } from '../settings/mailQueries'
import { sortDestinations } from './filingOrder'
import { shortcutHint } from './inboxShortcuts'

export type FilingRequest = { kind: 'folder' | 'label'; serial: number }
export function FilingMenu({ ids, messages = [], onDone, compact = false, request, showShortcuts = false }: { ids: string[]; messages?: MessageListItemOut[]; onDone?: () => void; compact?: boolean; request?: FilingRequest; showShortcuts?: boolean }) {
  const { texts, locale } = useI18n(), t = texts.mailFiling
  const query = useQuery(foldersQuery()), client = useQueryClient()
  const [busy, setBusy] = useState(false), [error, setError] = useState(false), [done, setDone] = useState(false)
  const [mode, setMode] = useState<'all' | 'folder' | 'label'>('all')
  useEffect(() => { if (request) setMode(request.kind) }, [request])
  const assign = async (change: Omit<FilingIn, 'message_ids'>) => {
    if (!ids.length || busy) return
    setBusy(true); setError(false); setDone(false)
    try {
      await unwrap(mailFilingAssign({ body: { message_ids: ids, ...change }, throwOnError: true }))
      void client.invalidateQueries({ queryKey: ['messages'] }); void client.invalidateQueries({ queryKey: ['message'] }); void client.invalidateQueries({ queryKey: ['summary'] }); void client.invalidateQueries({ queryKey: ['mail-quick-actions'] })
      setDone(true)
      onDone?.()
    } catch { setError(true) } finally { setBusy(false) }
  }
  const rows = sortDestinations(query.data ?? [], locale)
  const hasState = messages.length === ids.length && ids.length > 0
  const allInFolder = (id: string | null) => hasState && messages.every(mail => (mail.app_folder_id ?? null) === id)
  const labelCount = (id: string) => messages.filter(mail => (mail.app_label_ids ?? []).includes(id)).length
  const shortcuts = `${shortcutHint('folder', locale)} / ${shortcutHint('label', locale)}`
  return <><Menu openRequest={request?.serial} onOpen={() => setMode('all')} trigger={props => compact ? <IconButton {...props} icon="folder" label={t.file} shortcut={showShortcuts ? shortcuts : undefined} size="lg" disabled={!ids.length || busy || query.isPending || query.isError} /> : <Tooltip content={showShortcuts ? `${t.file} (${shortcuts})` : t.file}><Button {...props} variant="secondary" disabled={!ids.length || busy || query.isPending || query.isError}>{t.file}</Button></Tooltip>} items={[
    ...(mode !== 'label' ? [{ label: t.clearFolder, checked: hasState ? allInFolder(null) : undefined, onSelect: () => { void assign({ folder_id: null }) } }] : []),
    ...rows.filter(row => mode !== 'label' && row.kind === 'folder' && row.id).map(row => ({ label: `${t.folder}: ${row.name}`, icon: 'folder' as const, shortcut: showShortcuts ? shortcutHint('folder', locale) : undefined, checked: hasState ? allInFolder(row.id!) : undefined, onSelect: () => { void assign({ folder_id: row.id }) } })),
    ...rows.filter(row => mode !== 'folder' && row.kind === 'label' && row.id).map(row => {
      const count = labelCount(row.id!), all = hasState && count === ids.length
      return { label: `${t.label}: ${count > 0 && !all ? t.someLabel(row.name) : row.name}`, icon: 'tag' as const, shortcut: showShortcuts ? shortcutHint('label', locale) : undefined, checked: hasState ? count > 0 && !all ? 'mixed' as const : all : undefined, checkable: 'checkbox' as const, disabled: mode === 'label' && all,
        onSelect: () => { void assign(all && mode === 'all' ? { remove_labels: [row.id!] } : { add_labels: [row.id!] }) } }
    }),
  ]} />{error && <Notice tone="danger">{t.error}</Notice>}{done && <span role="status" className="le-sr">{t.done}</span>}</>
}
