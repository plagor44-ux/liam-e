import type { MailFolder } from '../api/generated/types.gen'
import { Tag, Tooltip } from '../ds'
import { useI18n } from '../i18n/I18n'
import { sortDestinations } from './filingOrder'

export function FilingTags({ folderId, labelIds, destinations, focusable = true }: { folderId?: string | null; labelIds?: string[]; destinations: MailFolder[]; focusable?: boolean }) {
  const { texts, locale } = useI18n()
  const assigned = new Set([folderId, ...(labelIds ?? [])].filter(Boolean))
  const rows = sortDestinations(destinations.filter(row => row.id && assigned.has(row.id)), locale)
  if (!rows.length) return null
  return <div className="le-filing-tags">{rows.map(row => {
    const name = `${row.kind === 'folder' ? texts.mailFiling.folder : texts.mailFiling.label}: ${row.name}`
    return <Tooltip key={row.id} content={name}><span className="le-filing-tag-anchor" role="img" aria-label={name} tabIndex={focusable ? 0 : undefined}><Tag tone="on-surface" icon={row.kind === 'folder' ? 'folder' : 'tag'} className={row.color && row.color !== 'none' ? `le-destination-${row.color}` : ''}><span>{name}</span></Tag></span></Tooltip>
  })}</div>
}
