import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { mailFilingAssign, mailFilingQuickActions } from '../api/generated/sdk.gen'
import { unwrap } from '../api/unwrap'
import { Button, Notice } from '../ds'
import { useI18n } from '../i18n/I18n'
import { useBreakpoint } from '../shell/useBreakpoint'
import { sortDestinations } from './filingOrder'

export const quickActionsQuery = () => ({ queryKey: ['mail-quick-actions'], queryFn: () => unwrap(mailFilingQuickActions({ throwOnError: true })), staleTime: 30_000, refetchOnWindowFocus: 'always' as const })

export function QuickActions({ messageId, showEmpty = false, onSuccess }: { messageId: string; showEmpty?: boolean; onSuccess?: () => void }) {
  const { texts, locale } = useI18n(), t = texts.mailFiling
  const client = useQueryClient(), query = useQuery(quickActionsQuery()), breakpoint = useBreakpoint()
  const [busy, setBusy] = useState(false), [error, setError] = useState(false)
  const rows = query.data ?? []
  const order = new Map(sortDestinations(rows.map(row => row.destination), locale).map((row, index) => [row.id, index]))
  const actions = [...rows].sort((a, b) => b.score - a.score || (order.get(a.destination.id) ?? 0) - (order.get(b.destination.id) ?? 0))
    .slice(0, breakpoint === 'mobile' ? 2 : breakpoint === 'tablet' ? 3 : 5)
  const file = async (id: string, kind: string) => {
    if (busy) return
    setBusy(true); setError(false)
    try {
      await unwrap(mailFilingAssign({ body: { message_ids: [messageId], ...(kind === 'folder' ? { folder_id: id } : { add_labels: [id] }) }, throwOnError: true }))
      onSuccess?.()
      for (const key of ['messages', 'message', 'summary', 'mail-quick-actions']) void client.invalidateQueries({ queryKey: [key] })
    } catch { setError(true); void client.invalidateQueries({ queryKey: ['mail-quick-actions'] }) } finally { setBusy(false) }
  }
  if (query.isError && !showEmpty && !error && !actions.length) return null
  if (!actions.length && !error && !query.isError) return showEmpty && !query.isPending ? <p className="le-hint">{t.quickEmpty}</p> : null
  return <div className="le-quick-actions" role="group" aria-label={t.quickActions}>
    {actions.map(({ destination }) => destination.id && <Button key={destination.id} variant="secondary" icon={destination.kind === 'folder' ? 'folder' : 'tag'} disabled={busy} onClick={event => { event.stopPropagation(); void file(destination.id!, destination.kind) }}>{destination.kind === 'folder' ? t.folder : t.label}: {destination.name}</Button>)}
    {(error || query.isError) && <Notice tone="danger" action={{ label: texts.chat.retry, onClick: () => { setError(false); void query.refetch() } }}>{t.quickError}</Notice>}
  </div>
}
