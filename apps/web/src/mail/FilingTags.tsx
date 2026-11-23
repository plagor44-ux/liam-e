import type { MailFolder } from '../api/generated/types.gen'
import { Tag } from '../ds'
import { useI18n } from '../i18n/I18n'
import { sortDestinations } from './filingOrder'

export function FilingTags({ folderId, labelIds, destinations }: { folderId?: string | null; labelIds?: string[]; destinations: MailFolder[] }) {
  const { texts, locale } = useI18n()
  const assigned = new Set([folderId, ...(labelIds ?? [])].filter(Boolean))
  const rows = sortDestinations(destinations.filter(row => row.id && assigned.has(row.id)), locale)
  if (!rows.length) return null
  return <div className="le-filing-tags">{rows.map(row => <Tag key={row.id} tone="on-surface" icon={row.kind === 'folder' ? 'folder' : 'tag'}><span>{row.kind === 'folder' ? texts.mailFiling.folder : texts.mailFiling.label}: {row.name}</span></Tag>)}</div>
}
