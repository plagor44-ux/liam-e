import type { MailFolder } from '../api/generated/types.gen'
import { Tag } from '../ds'
import { useI18n } from '../i18n/I18n'

export function FilingTags({ folderId, labelIds, destinations }: { folderId?: string | null; labelIds?: string[]; destinations: MailFolder[] }) {
  const { texts } = useI18n()
  const assigned = new Set([folderId, ...(labelIds ?? [])].filter(Boolean))
  const rows = destinations.filter(row => row.id && assigned.has(row.id))
  if (!rows.length) return null
  return <div className="le-filing-tags">{rows.map(row => <Tag key={row.id} tone="on-surface">{row.kind === 'folder' ? texts.mailFiling.folder : texts.mailFiling.label}: {row.name}</Tag>)}</div>
}
