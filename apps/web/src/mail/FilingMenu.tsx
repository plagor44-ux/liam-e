// i18n-exempt: API keys and query keys.
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { mailFilingAssign } from '../api/generated/sdk.gen'
import type { FilingIn } from '../api/generated/types.gen'
import { unwrap } from '../api/unwrap'
import { Button, Menu, Notice } from '../ds'
import { useI18n } from '../i18n/I18n'
import { foldersQuery } from '../settings/mailQueries'

export function FilingMenu({ ids, onDone }: { ids: string[]; onDone?: () => void }) {
  const { texts } = useI18n(), t = texts.mailFiling
  const query = useQuery(foldersQuery()), client = useQueryClient()
  const [busy, setBusy] = useState(false), [error, setError] = useState(false), [done, setDone] = useState(false)
  const assign = async (change: Omit<FilingIn, 'message_ids'>) => {
    if (!ids.length || busy) return
    setBusy(true); setError(false); setDone(false)
    try {
      await unwrap(mailFilingAssign({ body: { message_ids: ids, ...change }, throwOnError: true }))
      void client.invalidateQueries({ queryKey: ['messages'] }); void client.invalidateQueries({ queryKey: ['message'] }); void client.invalidateQueries({ queryKey: ['summary'] })
      setDone(true)
      onDone?.()
    } catch { setError(true) } finally { setBusy(false) }
  }
  const rows = query.data ?? []
  return <><Menu trigger={props => <Button {...props} variant="secondary" disabled={!ids.length || busy || query.isPending || query.isError}>{t.file}</Button>} items={[
    { label: t.clearFolder, onSelect: () => { void assign({ folder_id: null }) } },
    ...rows.filter(row => row.kind === 'folder' && row.id).map(row => ({ label: row.name, onSelect: () => { void assign({ folder_id: row.id }) } })),
    ...rows.filter(row => row.kind === 'label' && row.id).flatMap(row => [
      { label: t.addLabel(row.name), onSelect: () => { void assign({ add_labels: [row.id!] }) } },
      { label: t.removeLabel(row.name), onSelect: () => { void assign({ remove_labels: [row.id!] }) } },
    ]),
  ]} />{error && <Notice tone="danger">{t.error}</Notice>}{done && <span role="status" className="le-sr">{t.done}</span>}</>
}
