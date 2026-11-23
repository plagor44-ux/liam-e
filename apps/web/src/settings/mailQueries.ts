// i18n-exempt: query keys.
import { queryOptions } from '@tanstack/react-query'
import { mailFoldersList, mailFoldersPage } from '../api/generated/sdk.gen'
import type { DestinationPage, MailFolder } from '../api/generated/types.gen'
import { unwrap } from '../api/unwrap'

export const foldersQuery = () => queryOptions({ queryKey: ['mail-folders'], queryFn: () => unwrap(mailFoldersList({ throwOnError: true })) })

// Fetch bounded pages before sorting by the active display locale or filtering in settings.
export const folderInventoryQuery = () => queryOptions({ queryKey: ['mail-folders', 'inventory'], queryFn: async ({ signal }) => {
  const rows: MailFolder[] = [], seen = new Set<string>()
  let cursor: string | null = null
  do {
    const page: DestinationPage = await unwrap(mailFoldersPage({ query: { cursor, limit: 100 }, signal, throwOnError: true }))
    rows.push(...page.items)
    cursor = page.next_cursor
    if (cursor && seen.has(cursor)) throw new Error('Repeated inventory cursor')
    if (cursor) seen.add(cursor)
  } while (cursor)
  return rows
} })
